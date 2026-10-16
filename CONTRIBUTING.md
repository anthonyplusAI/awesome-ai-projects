# Help us find the next great AI project ✨

The main collection in [`data/projects.json`](data/projects.json) is selected after checking project fit, public source, and license. The weekly workflow also publishes dated discovery snapshots. Those are leads to investigate, not endorsements or automatic additions to the collection.

## Where the leads come from

| Source | Saved in | What it tells us |
| --- | --- | --- |
| [GitHub Trending, monthly](https://github.com/trending?since=monthly) | [`data/trending-monthly.json`](data/trending-monthly.json) | Every repository shown on that page, in page order, with GitHub's displayed monthly star gains. This raw page includes non-AI projects. |
| GitHub repository API | [`data/radar.json`](data/radar.json) | A small automated shortlist from monthly Trending, checked for an AI topic or description, a recognized open-source license, and public, non-fork, unarchived source. A curator still checks the actual project and license scope. |
| Product Hunt's [AI Atom feed](https://www.producthunt.com/feed?category=artificial-intelligence) **and** [open-source Atom feed](https://www.producthunt.com/feed?category=open-source) | [`data/producthunt-recent.json`](data/producthunt-recent.json) | Recent posts appearing in both feeds. Repository evidence comes from the product page when accessible, or strictly from the feed's direct GitHub outbound link. Atom `published` timestamps are labeled as feed dates, not launch dates. The feeds expose bounded recent slices, so `--days 90` is a lookback limit, not a promise of complete 90-day coverage. |
| GitHub repository API | [`data/stats.json`](data/stats.json) | Current total stars and GitHub license metadata for the selected main collection, with documented exceptions where GitHub cannot identify the license. |

The optional `python3 scripts/discover.py --days 90` command suggests recently created repositories from GitHub AI topic searches. It does not add projects. Monthly Trending rank and Product Hunt placement are discovery signals; neither proves that a repository is a good fit.

The [weekly workflow](.github/workflows/refresh.yml) refreshes these snapshots and regenerates the README on Mondays. Each snapshot has a fetch timestamp. A GitHub source failure stops publication. If Product Hunt cannot produce a complete snapshot, the workflow retains its last verified snapshot while refreshing the GitHub sections; check each date before interpreting a list as current.

## Nominate a project

Open an issue with the GitHub repository link, a one-sentence explanation of what someone can do with it, its source-code license, and any relevant demo or Product Hunt launch. You can also propose a pull request adding one entry to `data/projects.json`.

We look for projects whose public repository contains the working source code and declares a clear open-source software license. The project should use AI directly or make an AI workflow meaningfully better, and provide a useful README, demo, or installation path. We do not add forks, mirrors, archived repositories, or projects with unclear or noncommercial source-code terms. Model weights, datasets, and hosted services can have different licenses; mention those limits when relevant.

Use an existing category ID and describe the project in one concrete sentence:

```json
{
  "repo": "owner/repository",
  "name": "Project Name",
  "category": "agents-coding",
  "description": "One concrete sentence about what people can build or do.",
  "product_hunt": "https://www.producthunt.com/products/example"
}
```

Include `product_hunt` only when that listing points to the same GitHub project. Omit the field otherwise. Keep the new entry near related projects; the order in `data/projects.json` controls the main collection's order.

If GitHub reports `NOASSERTION` even though the repository's LICENSE clearly grants a recognized open-source license, document a manually checked exception on that project:

```json
"license_override": {
  "spdx_id": "MIT",
  "evidence_url": "https://github.com/owner/repository/blob/main/LICENSE"
}
```

Use this only after reading the license file and any listed third-party terms. The refresh uses the documented SPDX ID only while GitHub's API cannot identify one.

## Run the refresh locally

Use Python 3.12 or newer. Set `GITHUB_TOKEN` or `GH_TOKEN` for GitHub API requests; the repository's workflow supplies `GITHUB_TOKEN` automatically.

```sh
python3 -m unittest discover -s tests -v
python3 scripts/refresh.py --refresh
python3 scripts/trending.py --output data/trending-monthly.json
python3 scripts/radar.py --input data/trending-monthly.json --output data/radar.json
ph_candidate=$(mktemp)
python3 scripts/producthunt.py --days 90 --output "$ph_candidate"
python3 scripts/accept_producthunt.py --input "$ph_candidate" --output data/producthunt-recent.json
rm -f "$ph_candidate"
python3 scripts/render.py --write
python3 scripts/render.py --check
```

The commands fetch live sources, validate the data, and update generated README sections. The acceptance step keeps the previous Product Hunt snapshot if the new one is incomplete or fails validation. Do not edit the generated sections or snapshots by hand. Review a nomination's source, licensing, and practical usefulness before adding it to `data/projects.json`.
