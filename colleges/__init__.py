"""US college job-board scanner for the job dashboard.

    colleges/institutions.py   IPEDS master list (every US postsecondary institution)
    colleges/discover.py       crawl each institution's site, find its job board, classify the ATS
    colleges/fetchers/*.py     one module per higher-ed ATS / aggregator -> scrape.Job rows
    colleges/registry.py       data/colleges.json (scan input) + data/colleges_registry.json (full crawl record)

The fetchers plug into scrape.FETCHERS; data/colleges.json is merged into the
company list by scrape.load_companies(). See colleges/README.md.
"""
