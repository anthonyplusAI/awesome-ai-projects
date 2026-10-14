# Suggest a project ✨

Found an open-source AI project that deserves a spot? Open a pull request with one entry in [`data/projects.json`](data/projects.json), or open an issue with the links and a short explanation.

## What makes a good pick

- The public GitHub repository contains the working project's source code and declares an open-source software license.
- AI is part of what the project does, or the project makes an AI workflow meaningfully better. Clearly label adjacent tools, like agent-controllable creative software.
- The project has a useful README, demo, or installation path. Recent maintenance is a plus.
- Its description says what someone can actually do with it in one short sentence.
- A Product Hunt URL is welcome when that listing points to the same GitHub project. Leave the URL out if the connection is uncertain.

Forks, mirrors, and repositories without a clear software license are not added. Model weights and datasets can carry terms separate from source code; mention that when it matters.

## Add it to the catalog

Use an existing category ID and add an object to the `projects` array:

```json
{
  "repo": "owner/repository",
  "name": "Project Name",
  "category": "agents-coding",
  "description": "One concrete sentence about what it helps people do.",
  "product_hunt": "https://www.producthunt.com/products/example"
}
```

Omit `product_hunt` when there is no verified listing. Keep the entry near related projects; the order in the JSON controls the order in the README.

Then run:

```sh
python3 scripts/refresh.py --refresh
python3 scripts/refresh.py --check
```

The refresh command uses the GitHub API. It accepts `GITHUB_TOKEN` or `GH_TOKEN` from your environment for higher rate limits. It updates the checked-in star snapshot and regenerates the catalog section of the README. Avoid editing that generated section by hand.

## Help with discovery

`python3 scripts/discover.py --days 90` prints recently created candidates from GitHub topic searches. You can also browse [GitHub Trending](https://github.com/trending) and Product Hunt's [AI](https://www.producthunt.com/topics/artificial-intelligence?order=recent_launches) and [open-source](https://www.producthunt.com/topics/open-source?order=recent_launches) feeds. These are leads for human review; a high star count or launch rank alone does not guarantee a good fit.
