import pandas as pd
import numpy as np
import glob
import os
import subprocess
import contextlib
import multiprocessing as mp
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from matplotlib.colors import hsv_to_rgb
import seaborn as sns
from cycler import cycler

from . import stats, annotation
from . import plot as qtl_plot
from . import genotype as gt


@contextlib.contextmanager
def cd(cd_path):
    saved_path = os.getcwd()
    os.chdir(cd_path)
    yield
    os.chdir(saved_path)


def _samtools_depth_wrapper(args):
    """
    Wrapper for `samtools depth`.

    For files on GCP, GCS_OAUTH_TOKEN must be set.
    This can be done with qtl.refresh_gcs_token().
    """
    bam_file, region_str, sample_id, bam_index_dir, depth = args

    cmd = f'export GCS_OAUTH_TOKEN=$GCS_OAUTH_TOKEN; samtools depth -a -a -d {depth} -Q 255 -r {region_str} {bam_file}'
    if bam_index_dir is not None:
        with cd(bam_index_dir):
            c = subprocess.check_output(cmd, shell=True).decode().strip().split('\n')
    else:
        c = subprocess.check_output(cmd, shell=True).decode().strip().split('\n')

    df = pd.DataFrame([i.split('\t') for i in c], columns=['chr', 'pos', sample_id])
    df.index = df['chr']+'_'+df['pos']
    return df[sample_id].astype(np.int32)


def samtools_depth(region_str, bam_s, bam_index_dir=None, d=100000, num_threads=12):
    """
      region_str: string in 'chr:start-end' format
      bam_s: pd.Series or dict mapping sample_id->bam_path
      bam_index_dir: directory containing local copies of the BAM/CRAM indexes
    """
    pileups_df = []
    with mp.Pool(processes=num_threads) as pool:
        for k,r in enumerate(pool.imap(_samtools_depth_wrapper, [(i,region_str,j,bam_index_dir,d) for j,i in bam_s.items()]), 1):
            print(f'\r  * running samtools depth on region {region_str} for bam {k}/{len(bam_s)}', end='')
            pileups_df.append(r)
        print()
    pileups_df = pd.concat(pileups_df, axis=1)
    pileups_df.index.name = 'position'
    return pileups_df


def norm_pileups(pileups_df, libsize_s, covariates_df=None, id_map=lambda x: '-'.join(x.split('-')[:2])):
    """
      pileups_df: output from samtools_depth()
      libsize_s: pd.Series mapping sample_id->library size (total mapped reads)
    """
    # convert pileups to reads per million
    pileups_rpm_df = pileups_df / libsize_s[pileups_df.columns] * 1e6
    pileups_rpm_df.rename(columns=id_map, inplace=True)

    if covariates_df is not None:
        residualizer = stats.Residualizer(covariates_df)
        pileups_rpm_df = residualizer.transform(pileups_rpm_df)

    return pileups_rpm_df


def group_pileups(pileups_df, libsize_s, variant_id, genotypes, covariates_df=None,
                  id_map=lambda x: '-'.join(x.split('-')[:2])):
    """
      pileups_df: output from samtools_depth()
      libsize_s: pd.Series mapping sample_id->library size (total mapped reads)
    """
    pileups_rpm_df = norm_pileups(pileups_df, libsize_s, covariates_df=covariates_df, id_map=id_map)

    # get genotype dosages
    if isinstance(genotypes, str) and genotypes.endswith('.vcf.gz'):
        g = gt.get_genotype(variant_id, genotypes)[pileups_rpm_df.columns]
    elif isinstance(genotypes, pd.Series):
        g = genotypes
    else:
        raise ValueError('Unsupported format for genotypes.')

    # average pileups by genotype or category
    cols = np.unique(g[g.notnull()]).astype(int)
    df = pd.concat([pileups_rpm_df[g[g == i].index].mean(axis=1).rename(i) for i in cols], axis=1)
    return df


def plot(pileup_dfs, gene, mappability_bigwig=None, variant_id=None, order='additive',
         title=None, show_variant_pos=False, max_intron=300, alpha=1, lw=0.5,
         highlight_introns=None, highlight_introns2=None, shade_range=None,
         ymax=None, xlim=None, rasterized=False, outline=False, labels=None,
         dl=0.75, aw=4.5, dr=0.5, db=0.5, ah=1.5, dt=0.25, ds=0.2):
    """
      pileup_dfs:
    """

    if isinstance(pileup_dfs, pd.DataFrame):
        pileup_dfs = [pileup_dfs]
    num_pileups = len(pileup_dfs)

    nt = len(gene.transcripts)
    da = 0.08 * nt + 0.01*(nt-1)
    da2 = 0.12

    fw = dl + aw + dr
    fh = db + da + ds + (num_pileups-1)*da2 + num_pileups*ah + dt
    if mappability_bigwig is not None:
        fh += da2

    if variant_id is not None:
        chrom, pos, ref, alt = variant_id.split('_')[:4]
        pos = int(pos)
        if isinstance(pileup_dfs[0].columns[0], int):
            gtlabels = np.array([
                f'{ref}{ref}',
                f'{ref}{alt}',
                f'{alt}{alt}',
            ])
        else:
            gtlabels = None
    else:
        pos = None
        gtlabels = None

    if pileup_dfs[0].shape[1] <= 3:
        custom_cycler = cycler('color', [
            hsv_to_rgb([0.55, 0.75, 0.8]),  #(0.2, 0.65, 0.8),  # blue
            hsv_to_rgb([0.08, 1, 1]),  #(1.0, 0.5, 0.0),   # orange
            hsv_to_rgb([0.3, 0.7, 0.7]),  #(0.2, 0.6, 0.17),  # green
        ])
    else:
        custom_cycler = None

    fig = plt.figure(facecolor=(1,1,1), figsize=(fw,fh))
    ax = fig.add_axes([dl/fw, (db+da+ds)/fh, aw/fw, ah/fh])
    ax.set_prop_cycle(custom_cycler)
    axv = [ax]
    for i in range(1, num_pileups):
        ax = fig.add_axes([dl/fw, (db+da+ds+i*(da2+ah))/fh, aw/fw, ah/fh], sharex=axv[0])
        ax.set_prop_cycle(custom_cycler)
        axv.append(ax)

    s = pileup_dfs[0].sum()
    if isinstance(order, list):
        sorder = order
    elif order == 'additive':
        sorder = s.index
        if s[sorder[0]] < s[sorder[-1]]:
            sorder = sorder[::-1]
    elif order == 'sorted':
        sorder = np.argsort(s)[::-1]
    elif order == 'none':
        sorder = s.index

    gene.set_plot_coords(max_intron=max_intron)
    xi = gene.map_pos(np.arange(gene.start_pos, gene.end_pos+1))

    for k,ax in enumerate(axv):
        for i in sorder:
            if i in pileup_dfs[k]:
                if outline:
                    ax.plot(xi, pileup_dfs[k][i], label=i, lw=lw, alpha=alpha, rasterized=rasterized)
                else:
                    ax.fill_between(xi, pileup_dfs[k][i], label=i, alpha=alpha, rasterized=rasterized)

    if labels is None:
        labels = ['Mean RPM'] * num_pileups
    for k,ax in enumerate(axv):
        ax.margins(0)
        ax.set_ylabel(labels[k], fontsize=12)
        qtl_plot.format_plot(ax, fontsize=10, lw=0.6)
        ax.tick_params(axis='x', length=3, width=0.6, pad=1)
        ax.set_xticks(gene.map_pos(gene.get_collapsed_coords().reshape(1,-1)[0]))
        ax.set_xticklabels([])
        ax.spines['left'].set_position(('outward', 6))

    if xlim is not None:
        ax.set_xlim(xlim)
    if ymax is not None:
        ax.set_ylim([0, ymax])

    if gtlabels is None:
        leg = axv[-1].legend(loc='lower left', labelspacing=0.15, frameon=False, fontsize=9, borderaxespad=0.5,
                             borderpad=0, handlelength=0.75, bbox_to_anchor=(0,1.05), ncol=3)
    else:
        leg = axv[-1].legend(loc='upper left', labelspacing=0.15, frameon=False, fontsize=9, borderaxespad=0.5,
                             borderpad=0, handlelength=0.75, labels=gtlabels[sorder])
    for line in leg.get_lines():
        line.set_linewidth(1)

    if variant_id is not None and title is None:
        axv[-1].set_title(f'{gene.name} :: {variant_id}', fontsize=11)
    else:
        axv[-1].set_title(title, fontsize=11)

    # highlight variant
    if show_variant_pos and pos is not None and pos >= gene.start_pos and pos <= gene.end_pos:
        x = gene.map_pos(pos)
        for ax in axv:
            xlim = np.diff(ax.get_xlim())[0]
            ylim = np.diff(ax.get_ylim())[0]
            h = 0.04 * ylim
            b = h/np.sqrt(3) * ah/aw * xlim/ylim
            v = np.array([[x-b, -h-0.01*ylim], [x+b, -h-0.01*ylim], [x, -0.01*ylim]])
            ax.add_patch(patches.Polygon(v, closed=True, color='r', clip_on=False, zorder=10))

    if shade_range is not None:
        if isinstance(shade_range, str):
            shade_range = shade_range.split(':')[-1].split('-')
        shade_range = np.array(shade_range).astype(int)
        shade_range -= gene.start_pos

        shade_range = ifct(shade_range)
        for k in range(len(shade_range)-1):
            axv[-1].add_patch(patches.Rectangle((shade_range[k], 0), shade_range[k+1]-shade_range[k], ax.get_ylim()[1],
                              facecolor=[0.8]*3 if k % 2 == 0 else [0.9]*3, zorder=-10))

    # add gene model
    gax = fig.add_axes([dl/fw, db/fh, aw/fw, da/fh], sharex=axv[0])
    gene.plot(ax=gax, max_intron=max_intron, wx=0.1, highlight_introns=highlight_introns,
              highlight_introns2=highlight_introns2, fc='k', ec='none', clip_on=True)
    gax.set_title('')
    gax.set_ylabel('Isoforms', fontsize=10, rotation=0, ha='right', va='center')
    plt.setp(gax.get_xticklabels(), visible=False)
    plt.setp(gax.get_yticklabels(), visible=False)
    for s in ['top', 'right', 'bottom', 'left']:
        gax.spines[s].set_visible(False)
    gax.tick_params(length=0, labelbottom=False)
    axv.append(gax)

    if mappability_bigwig is not None:  # add mappability
        c = gene.get_coverage(mappability_bigwig)
        mpax = fig.add_axes([dl/fw, 0.25/fh, aw/fw, da2/fh], sharex=axv[0])
        mpax.fill_between(xi, c, color=3*[0.6], lw=1, interpolate=False, rasterized=rasterized)
        for i in ['top', 'right']:
            mpax.spines[i].set_visible(False)
            mpax.spines[i].set_linewidth(0.6)
        mpax.set_ylabel('Map.', fontsize=10, rotation=0, ha='right', va='center')
        mpax.tick_params(length=0, labelbottom=False)
        axv.append(mpax)
        plt.sca(axv[0])

    # axv[-1].set_xlabel(f'Exon coordinates on {gene.chr}', fontsize=12)

    return axv
