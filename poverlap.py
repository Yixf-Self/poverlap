#!/usr/bin/env python
import sys
import os
from multiprocessing import cpu_count
from multiprocessing.dummy import Pool
from toolshed import nopen, reader
from tempfile import mktemp as _mktemp
import atexit
from commandr import command, Run
import json

from signal import signal, SIGPIPE, SIG_DFL
signal(SIGPIPE,SIG_DFL)

NCPUS = cpu_count()
if NCPUS > 4: NCPUS -= 1

SEP = "&*#Z"

def mktemp(*args, **kwargs):
    def rm(f):
        try: os.unlink(f)
        except OSError: pass

    if not 'suffix' in kwargs: kwargs['suffix'] = ".bed"
    f = _mktemp(*args, **kwargs)
    atexit.register(rm, f)
    return f


def run(cmd):
    return list(nopen("|%s" % cmd.lstrip("|")))[0]


def extend_bed(fin, fout, bases):
    # we're extending both a.bed and b.bed by this distance
    # so divide by 2.
    bases /= 2
    with nopen(fout, 'w') as fh:
        for toks in (l.rstrip("\r\n").split("\t") for l in nopen(fin)):
            toks[1] = max(0, int(toks[1]) - bases)
            toks[2] = max(0, int(toks[2]) + bases)
            if toks[1] > toks[2]:  # negative distances
                toks[1] = toks[2] = (toks[1] + toks[2]) / 2
            assert toks[1] <= toks[2]
            print >>fh, "\t".join(map(str, toks))
    return fh.name


@command('fixle')
def fixle(bed, atype, btype, type_col=4, metric='wc -l', n=100):
    """\
    from Haiminen et al in BMC Bioinformatics 2008, 9:336
    `bed` may contain, e.g. 20 TFBS as defined by the type in `type_col`
    we keep the rows labeled as `atype` in the same locations, but we randomly
    assign `btype` to any of the remaining rows.
    Arguments:
        bed - BED file with a column that delineates types
        atype - the query type, e.g. Pol2
        btype - the type to be shuffled, e.g. CTCF
        type_col - the column in `bed` the lists the types
        n - number of shuffles
        metric - a string that indicates a program that consumes BED intervals
    """
    type_col -= 1
    n_btypes = 0
    pool = Pool(NCPUS)
    with nopen(mktemp(), 'w') as afh, \
            nopen(mktemp(), 'w') as ofh, \
            nopen(mktemp(), 'w') as bfh:
        for toks in (l.rstrip("\r\n").split("\t") for l in nopen(bed)):
            if toks[type_col] == atype:
                print >> afh, "\t".join(toks)
            else:
                print >> ofh, "\t".join(toks)
                if toks[type_col] == btype:
                    print >>bfh, "\t".join(toks)
                    n_btypes += 1
    assert n_btypes > 0, ("no intervals found for", btype)

    a, b, other = afh.name, bfh.name, ofh.name
    orig_cmd = "bedtools intersect -wa -a {a} -b {b} | {metric}".format(**locals())
    observed = int(run(orig_cmd))
    res = {"observed": observed }
    script = __file__
    bsample = '<(python {script} bed-sample {other} --n {n_btypes})'.format(**locals())
    shuf_cmd = "bedtools intersect -wa -a {a} -b {bsample} | {metric}".format(**locals())
    res['shuffle_cmd'] = shuf_cmd
    res['metric'] = metric
    sims = [int(x) for x in pool.imap(run, [shuf_cmd] * n)]
    res['simulated mean metric'] = "%.1f" % (sum(sims) / float(len(sims)))
    res['simulated_p'] = (sum((s >= observed) for s in sims) / float(len(sims)))
    res['sims'] = sims
    return json.dumps(res)


@command('bed-sample')
def bed_sample(bed, n=100):
    """\
    choose n random lines from a bed file. uses reservoir sampling
    Arguments:
        bed - a bed file
        n - number of lines to sample
    """
    n = int(n)
    from random import randint
    lines = []
    for i, line in enumerate(nopen(bed)):
        if i < n:
            lines.append(line)
        else:
            replace_idx = randint(0, i)
            if replace_idx < n:
                lines[replace_idx] = line
    print "".join(lines),


@command('local-shuffle')
def local_shuffle(bed, loc='500000'):
    """
    randomize the location of each interval in `bed` by moving it's
    start location to within `loc` bp of its current location or to
    it's containing interval in `loc`
    Arguments:
        bed - input bed file
        loc - shuffle intervals to within this distance (+ or -)
               if not an integer, then this should be a BED file containing
               regions such that each interval in `bed` is shuffled within
               its containing interval in `loc`
    """
    from random import randint
    if str(loc).isdigit():
        dist = abs(int(loc))
        for toks in (l.rstrip('\r\n').split('\t') for l in nopen(bed)):
            d = randint(-dist, dist)
            toks[1:3] = [str(max(0, int(loc) + d)) for loc in toks[1:3]]
            print "\t".join(toks)
    else:
        # we are using dist as the windows within which to shuffle
        assert os.path.exists(loc)
        bed4 = mktemp()
        with open(bed4, 'w') as fh:
            # this step is so we don't have to track the number of columns in a
            for toks in reader(bed, header=False):
                fh.write("%s\t%s\n" % ("\t".join(toks[:3]), SEP.join(toks)))

        missing = 0
        # we first find the b-interval that contains each a-interval by
        # using bedtools intersect
        for toks in reader("|bedtools intersect -wao -a {bed4} -b {loc}"
                .format(**locals()), header=False):
            ajoin = toks[:4]
            a = ajoin[3].split(SEP)  # extract the full interval
            b = toks[4:]

            if int(b[-1]) == 0:
                missing += 1
                continue
            assert a[0] == b[0], ('chroms dont match', a, b)

            alen = int(a[2]) - int(a[1])
            # doesn't care if the new interval is completely contained in b
            astart = randint(int(b[1]), int(b[2]))

            # subtract half the time.
            aend = (astart - alen) if randint(0, 1) == 0 and astart > alen \
                else (astart + alen)

            a[1], a[2] = map(str, (astart, aend) if astart < aend
                             else (aend, astart))

            print "\t".join(a)
        if missing > 0:
            print >> sys.stderr, ("found {missing} intervals in {bed} that "
                                  " were not contained in {loc}"
                                  .format(**locals()))


def zclude(bed, other, exclude=True):
    """
    include or exclude intervals from bed that overlap other
    if exclude is True:
        new = bedtools intersect -v -a bed -o other
    """
    if other is None: return bed
    n_orig = sum(1 for _ in nopen(bed))
    tmp = mktemp()
    if exclude:
        run("bedtools intersect -v -a {bed} -b {other} > {tmp}; echo 1"
            .format(**locals()))
    else:
        run("bedtools intersect -u -a {bed} -b {other} > {tmp}; echo 1"
            .format(**locals()))
    n_after = sum(1 for _ in nopen(tmp))
    clude = "exclud" if exclude else "includ"
    pct = 100 * float(n_orig - n_after) / n_orig
    print >>sys.stderr, ("reduced {bed} from {n_orig} to {n_after} "
             "{pct:.3f}% by {clude}ing {other}").format(**locals())
    return tmp


@command('poverlap')
def poverlap(a, b, genome=None, metric='wc -l', n=100, chrom=False, exclude=None,
             include=None, shuffle_both=False, overlap_distance=0,
             shuffle_loc=None):
    """\
    poverlap is the main function that parallelizes testing overlap between `a`
    and `b`. It performs `n` shufflings and compares the observed number of
    lines in the intersection to the simulated intersections to generate a
    p-value.
    When using shuffle_loc, `exclude`, `include` and `chrom` are ignored.
    Args that are not explicitly part of BEDTools are explained below, e.g. to
    find intervals that are within a given distance, rather than fully
    overlapping, one can set overlap_distance to > 0.
    To shuffle intervals within a certain distance of their current location,
    or to keep then inside a set of intervals, use shuffle_loc to retain the
    local structure.

    Arguments:
        a - first bed file
        b - second bed file
        genome - genome file
        metric - a string that indicates a program that consumes BED intervals
                 from STDIN and outputs a single, numerical value upon
                 completion. default is 'wc -l'
        n - number of shuffles
        chrom - shuffle within chromosomes
        exclude - optional bed file of regions to exclude
        include - optional bed file of regions to include
        shuffle_both - if set, both a and b are shuffled. normally just b
        overlap_distance - intervals within this distance are overlapping.
        shuffle_loc - shuffle each interval to a random location within this
                      distance of its current location. If not an integer,
                      then this should be a BED file containing regions such
                      that each interval in `bed` is shuffled within its
                      containing interval in `dist`
    """
    pool = Pool(NCPUS)
    assert os.path.exists(genome), (genome, "not available")

    n = int(n)
    chrom = "" if chrom is False else "-chrom"
    if genome is None: assert shuffle_loc

    # limit exclude and then to include
    a = zclude(zclude(a, exclude, True), include, False)
    b = zclude(zclude(b, exclude, True), include, False)

    exclude = "" if exclude is None else ("-excl %s" % exclude)
    include = "" if include is None else ("-incl %s" % include)

    if overlap_distance != 0:
        a = extend_bed(a, mktemp(), overlap_distance)
        b = extend_bed(b, mktemp(), overlap_distance)

    orig_cmd = "bedtools intersect -wa -a {a} -b {b} | {metric}".format(**locals())

    if shuffle_loc is None:
        # use bedtools shuffle
        if shuffle_both:
            a = "<(bedtools shuffle {exclude} {include} -i {a} -g {genome} {chrom})".format(**locals())
        shuf_cmd = ("bedtools intersect -wa -a {a} "
                "-b <(bedtools shuffle {exclude} {include} -i {b} -g {genome}"
                " {chrom}) | {metric} ".format(**locals()))
    else:
        # use python shuffle ignores --chrom and --genome
        script = __file__
        if shuffle_both:
            a = "<(python {script} local-shuffle {a} --loc {shuffle_loc})".format(**locals())
        shuf_cmd = ("bedtools intersect -wa -a {a} "
            "-b <(python {script} local-shuffle {b} --loc {shuffle_loc})"
            " | {metric}").format(**locals())

    #print "original command: %s" % orig_cmd
    observed = int(run(orig_cmd))
    res = {"observed": observed, "shuffle_cmd": shuf_cmd }
    sims = [int(x) for x in pool.imap(run, [shuf_cmd] * n)]
    res['metric'] = metric
    res['simulated mean metric'] = (sum(sims) / float(len(sims)))
    res['simulated_p'] = \
        (sum((s >= observed) for s in sims) / float(len(sims)))
    res['sims'] = sims
    return json.dumps(res)

if __name__ == "__main__":
    if "--ncpus" in sys.argv:
        i = sys.argv.index("--ncpus")
        sys.argv.pop(i)
        NCPUS = int(sys.argv.pop(i))
    res = Run()
