# Plant paper digest

A weekly list of new plant biology papers, filtered by your own keywords, published as a web page you can open on any device. It runs on GitHub for free: no installation, no server, no costs.

Every Monday it collects new papers from **Europe PMC** (PubMed and preprints), **bioRxiv** and **Crossref** (about 30 journals checked directly). It keeps the plant papers, scores them against your keywords, merges preprint and journal versions of the same paper, skips anything it already showed you, and sorts the rest into topic sections. On the page you can tick papers, filter by any word, hide preprints or reviews, copy the ticked papers as references, or download them as a RIS file for Zotero.

More background: [the blog post](https://milanzupunski.github.io/blog/2026/10/08/plant-paper-digest).

## Set it up (about five minutes)

You need a free GitHub account.

1. **Copy this repository.** Click the green **Use this template** button at the top of this page, then **Create a new repository**. Give it any name (for example `paper-digest`), leave it **Public**, and click **Create repository**.

2. **Turn on the web page.** In your new repository go to **Settings**, then **Pages** (left menu). Under *Build and deployment*, set **Source** to **GitHub Actions**. That's all; there is nothing to save.

3. **Put in your keywords.** Open `profiles.txt` in your repository, click the pencil icon to edit, change the keywords (see below), and click **Commit changes**. You can skip this for the first run and tune later.

4. **Run it once.** Go to the **Actions** tab. If GitHub asks, click the button to enable workflows. Click **Weekly digest** on the left, then **Run workflow**. For the first run, type `30` in the box to look back 30 days, and click the green **Run workflow** button.

5. **Open your digest.** The run takes 10 to 30 minutes. When it has a green check mark, open `https://YOUR-USERNAME.github.io/YOUR-REPOSITORY/`. Bookmark it, including on your phone.

From then on it runs by itself every Monday morning. Each week's digest stays available under *earlier digests*.

**Want a notification?** Add `https://YOUR-USERNAME.github.io/YOUR-REPOSITORY/feed.xml` to a feed reader (Feedly, Inoreader, or Zotero's feeds). It shows a new item every time a digest is ready.

## Your keywords

Everything is in `profiles.txt`, a plain text file with instructions at the top. It has these parts:

- **`[gate]`**: a paper must mention at least one of these terms to be considered at all (plant, Arabidopsis, a crop, seedling, chloroplast and so on). Add your organisms here.
- **`[section: ...]`**: each section becomes a heading in the digest. Put your own topics and terms here. A paper goes into the section where it scores highest.
- **`[boost]`** and **`[penalty]`**: terms that raise or lower the score without defining a section.
- **`[journal penalty]`**: journals or fields you want pushed down.
- **`[journals]`**: journals that Crossref checks directly, as `ISSN | name | plant/any`.

How terms work:

| You write | It matches |
|---|---|
| `root hair` | "root hair" and "root-hair" |
| `stoma*` | stoma, stomata, stomatal |
| `ROS` | only uppercase ROS (all-caps terms are case-sensitive) |
| <code>calcium &#124; 2</code> | weight 2 (default is 1) |
| `re:PIN\d+` | a regular expression, here PIN1, PIN2 and so on |

A term in the title counts double. Give broad words ("growth", "development") a low weight like 0.5 and the specific things you care about a high weight like 2 or 3.

## Tuning

- **Too many papers:** raise `min_score` in the `[settings]` part of `profiles.txt`.
- **A paper you expected is missing:** look at the "matched:" line under similar papers in the digest to see which terms fire, then add the missing term.
- **Old papers appear:** lower `max_age_days`.
- **A different day or time:** edit the `cron` line in `.github/workflows/digest.yml`. Times are UTC.
- **Start over:** delete `seen.json` (the list of papers already shown) and run the workflow again.
- **Something went wrong:** open the Actions tab and click the failed run to see the log. The bottom of every digest page also has a short run report.

## Good to know

- **Your digest is public**, like your repository. Web pages from private repositories need a paid GitHub plan.
- **GitHub pauses scheduled runs** in repositories with no activity for 60 days. The weekly digest itself counts as activity, so this should not happen, but if runs stop, go to the Actions tab and re-enable the workflow.
- **It's a keyword filter.** It will miss papers that use none of your terms, so it is worth adding synonyms and gene names over time.

## Getting updates

Your copy does not update itself when this template improves. To get a newer version, open `digest.py` here, copy its contents, and paste them over `digest.py` in your own repository (pencil icon, then **Commit changes**). Your keywords in `profiles.txt` are not affected.

## Run it on your own computer instead

You can also run it locally with Python 3 and the `requests` package (included in Anaconda): download the repository and run `python digest.py --days 30`. The digest appears in `digests/latest.html`. On Windows, `run_digest.bat` does the same and opens the result, and `setup_weekly_task.bat` schedules it every Monday.

---

Made by [Milan Župunski](https://milanzupunski.github.io). The idea and the testing were mine. The code and the first draft of this guide were written with the help of [Claude](https://claude.ai) by [Anthropic](https://www.anthropic.com).
