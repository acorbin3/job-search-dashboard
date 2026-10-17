# College job boards

Scans every US college's own job board (plus the faculty job aggregators) for
the dashboard, built for adjunct / instructor searches.

## How it works

1. `colleges/institutions.py` downloads the IPEDS directory (NCES `HD<year>.zip`):
   all ~6,000 US postsecondary institutions with their websites. By default the
   degree-granting sectors 1-6 plus the sector-0 system/district offices are used,
   and the <2-year sectors are kept only when the name says technical college /
   career center (~4,500 schools); beauty and trade schools are skipped. The IPEDS
   system name (F1SYSNAM) is kept so campuses can share a system board.
2. `python3 -m colleges.discover` visits each school's homepage, follows the
   jobs / careers / employment / HR links (and `jobs.<school>.edu`-style hosts),
   and classifies the landing page with the ATS detectors (page URL and every
   ATS-looking link on the page). With `--browser` a school that stays
   unclassified is crawled again in headless Chromium, which gets through
   Imperva/Incapsula challenge pages and JS-rendered HR pages (Cloudflare
   "Just a moment" walls still block). At the end campuses of one IPEDS system
   inherit a board found at the system office or at 2+ sister campuses. A school can have
   several boards (e.g. a Workday athletics site and a PageUp main site). Results
   go to `data/colleges_registry.json`; `data/colleges.json` is generated from it
   in the same shape as `data/companies.json`.
3. `scrape.py` merges `data/colleges.json` into every scan (`--no-colleges` to
   skip) and registers the higher-ed fetchers under `college_<name>` keys.
4. `tools/scan_colleges.py` is the daily job: rescans only the college boards,
   merges into `.raw_scan.json`, re-derives every tab, writes
   `reports/colleges_<date>.md` with never-seen-before postings, and (with
   `--notify`) posts a macOS notification. `tools/daily_college_scan.sh` is the
   launchd entry point (`~/Library/LaunchAgents/com.adamcorbin.college-job-scan.plist`,
   06:30 daily).

## Fetchers (`colleges/fetchers/`)

| key | platform | access |
|---|---|---|
| college_peopleadmin | PeopleAdmin / PowerSchool (`/postings/search.atom`) | Atom feed |
| college_pageup | PageUp classic (`/cw/en-us/rss`) and PageUp Clinch sites | RSS; Clinch needs a one-time Playwright cookie |
| college_neogov | NeoGov (governmentjobs.com / schooljobs.com) | HTML fragment endpoint |
| college_interfolio (also Interfolio-built sites on a school host, e.g. `facultyjobs.jhu.edu/positions` -> `/api/positions?page=&pageSize=100`), college_oracle, college_taleo, college_successfactors, college_interviewexchange, college_peoplesoft | see module docstrings | |
| college_paycor | Paycor Recruiting (`recruitingbypaycor.com/career/CareerHome.action?clientId=`) | HTML |
| college_jazzhr | JazzHR (`<slug>.applytojob.com/apply`) | HTML list + detail pages |
| college_taleobe | Taleo Business Edition (`<pod>.tbe.taleo.net/.../careers/v2/searchResults?org=`) | HTML, 10/page via the session "next" link |
| college_breezy | Breezy HR (`<slug>.breezy.hr`) | `/json` list + `/p/<id>` detail |
| college_jobscore | JobScore (`careers.jobscore.com/careers/<slug>`) | `/careers/<slug>/feed` JSON (description inline) |
| college_clearcompany | ClearCompany legacy (`<slug>.hrmdirect.com/employment/job-openings.php`) | HTML; the React `<slug>.clearcompany.com/careers` app is not handled |
| college_isolvedhire | isolved Hire / ApplicantPro white labels (`isolvedhire.com`, `applicantpool.com`, `applicantlist.com`, `workbrightats.com`, `aaimtrack.com`) | `/core/jobs/<domainId>` JSON + JSON-LD detail; stealth Playwright fallback when the isolvedhire WAF 503s |
| college_applicantstack | ApplicantStack (`<slug>.applicantstack.com/x/openings`) | HTML table + `/x/detail` pages |
| college_apscareer | APS Payroll (`<slug>.apscareerportal.com/jobs`) | HTML + detail pages |
| college_ukgready | UKG Ready (`secureN.saashr.com`, `secureN.entertimeonline.com`, `*.onehcm.com`, `*.yourpayrollhr.com`; `/ta/<id>.careers`) | `/ta/rest/ui/recruitment/companies/\|<id>/job-requisitions` JSON |
| college_appone | AppOne / Paychex (`www2.appone.com/Search/Search.aspx?ServerVar=`) | ClientID from the search page, then the Paychex "Browse all jobs" HTML + `MainInfoReq.asp` detail |
| college_applitrack | Frontline AppliTrack (`applitrack.com/<district>/onlineapp`) | `jobpostings/Output.asp?all=1` (document.write HTML, description inline) |
| college_silkroad | SilkRoad / Rival OpenHire (`<tenant>-openhire.silkroad.com/epostings`) and the newer `jobs.silkroad.com/<tenant>/<site>` portals (ExternalCareerSite, External, Careers, EmploymentPortal) | `app.allpositions` HTML + detail; portal: `/<site>/sitemap` links (paged `?page=N` list when the sitemap is capped at 100) + `/jobs/<id>` detail |
| college_schoolspring | PowerSchool Unified Talent / TalentEd (`<district>.tedk12.com` -> `<district>.schoolspring.com`) | `api.schoolspring.com/api/Jobs/GetPagedJobsWithSearch` + `/api/Jobs/<id>` JSON |
| college_myjobsadp | ADP Recruitment Management (`myjobs.adp.com/<domain>`, also where `recruiting.adp.com/srccar/.../RTI.home?c=` redirects) | career-site JSON for orgoid/token, then `my.adp.com/.../job-requisitions/apply-custom-filters` (no Accept-Language header or it returns 0) |
| college_teamtailor | Teamtailor (`<slug>.teamtailor.com` or a custom host loading `teamtailor-cdn.com`) | `/jobs.rss` (description inline) |
| college_trinethire | TriNet Hire (`app.trinethire.com/companies/<id>-<slug>/jobs`) | HTML table + detail pages |
| college_rippling | Rippling ATS (`ats.rippling.com/<slug>/jobs`) | `__NEXT_DATA__` list + detail |
| college_wpjobmanager | WP Job Manager on the school's WordPress (`/?feed=job_feed`) | RSS (content:encoded inline) |
| college_prismhr | PrismHR Hire / HiringThing (`<slug>.prismhr-hire.com`, `<slug>.hiringthing.com`, `<slug>.rippling-ats.com`) | HTML + `/job/<id>` detail |
| college_ashby | Ashby (`jobs.ashbyhq.com/<slug>`) | `api.ashbyhq.com/posting-api/job-board/<slug>` JSON (description inline) |
| college_brightmove | BrightMove (`j.brt.mv/ax.do?portalGK=`, `ejob.bz`, `<slug>.brightmove.com`) | `JobRSS.do?companyGK=&portalGK=` RSS (description inline); portal table + `PortalViewRequirement.do` detail when the ids are missing |
| college_attrax | Attrax career sites on a custom host (`/jobs?options=N&page=N`), e.g. Adtalem's careers.covista.com; detected by the `attraxbundle.js` / `attrax-vacancy-tile` markers | HTML tiles, 12/page, + `/job/<slug>` detail |
| college_catsone | CATS (`<slug>.catsone.com/careers/<portal>`) | HTML + detail pages |
| college_eightfold | Eightfold career hubs on a school host (`hiring.jhu.edu/careers`; `window._EF_GROUP_ID` / `pcsx` markers) | `/api/pcsx/search?domain=&start=&num=10` JSON (10/page) + `/api/apply/v2/jobs/<id>` detail |
| college_avature | Avature (`<tenant>.avature.net/careers/SearchJobs`) | HTML rows, `?jobOffset=N` (10/page) + `/careers/JobDetail/<slug>/<id>` detail |
| college_keldair | Keldair (`jobs.keldair.com/<instance>[/<subBrand>]`, also the citysuitehr / connect-ats / keldairaps / constructhire / strongpayhire white labels) | `job-gateway.<domain>/jobs?visibilityType=External` JSON with `X-InstanceIdentifier` header + `/jobs/<id>` detail |
| college_jobflows | JobFlows for monday.com (`go.jobflows.co/<slug>`) | `jobflows.jtpk.app/jobs/searchAndFilter/<slug>` JSON (description inline) |
| college_exacthire | ExactHire (`<tenant>.exacthire.com`) | public `api.exacthire.com/api/public/career_sites/<id>/positions?page=N` JSON |
| college_rss | plain RSS/Atom job feed on a school site (hand-recorded, e.g. careers.umich.edu `search/feed/advanced?...`) | feed items; description inline |
| college_trakstar | Trakstar Hire / RecruiterBox (`<slug>.hire.trakstar.com`, `jsapi.recruiterbox.com/v1/openings?client_name=`) | `jsapi.recruiterbox.com` JSON when the client name is known (description inline), else `/jobfeeds/<slug>` RSS; `/api/v2/openings/` needs an API key |
| college_powerschool | PowerSchool Applicant Tracking Enterprise (`<host>.atenterprise.powerschool.com/ats/job_board?COMPANY_ID=`) | HTML table, `start_index=` pages + `job_board_form?op=view` detail |
| college_winocular | WinOcular (`apps2.winocular.com/<district>/jobs/`) | `wSpace.exe?Action=wsJobsMain` table + `wsJobsDetail&JobPosting=` detail |
| college_willsub | ESS WillSub+ district boards (`jobs.willsubplus.com/jobs?districtIds=`) | `__NEXT_DATA__` jobPostingsList (description inline), `&page=N` |
| college_jics | Jenzabar JICS employment portlets (`/ICS/.../<Page>.jnz` with the `psSearch` form) | "Search positions" POST, then one `__doPostBack` per posting for the body (session viewstate) |
| college_focus | Focus School Software (`<district>.focusschoolsoftware.com/focus/jobs`) | `FocusModule.class.php` RPC (`getJobs`, `getJobPosting`) with the page's key/token/session id |
| college_infor | Infor CloudSuite HCM job boards (`<tenant>.inforcloudsuite.com/hcm/Jobs/page/JobsHomePage?csk.JobBoard=&csk.HROrganization=`) | session, then `list/JobPosting.SearchForJobsResults?pagesize=500` JSON + `form/<resourceId>.JobPostingDisplay` detail |
| college_nlx | NLx / DirectEmployers `.jobs` / `.careers` sites (`cuny.jobs`, `uagc.careers`; `seo.nlx.org` markers) | `prod-search-api.jobsyn.org/api/v1/solr/search?page=N` JSON with `x-origin: <host>` (description inline, 10/page) |
| college_brassring | IBM Kenexa BrassRing (`sjobs.brassring.com/TGnewUI/Search/Home/Home?partnerid=&siteid=`, legacy `cim_home.asp` links) | session Home, then `Ajax/ProcessSortAndShowMoreJobs` POST JSON (description inline, 50/page) |
| college_beamery | Beamery Grow / Umbraco career sites on a school host (`jobs.snhu.edu/en/jobs/`; `beamery.com` + `js-view-job` markers) | HTML cards, `?page=N` + detail pages |
| college_generic | any other board: posting-looking links on the discovered listing page | HTML |
| (generic options) | `"browser": true` loads the listing in headless Chromium (bot walls, JS lists); `"pdf_links": true` treats PDF/DOC links as postings (PDF-only boards); `"pages": [...]` extra listing URLs | |
| college_madgex | Inside Higher Ed Careers + Chronicle (keyword RSS) | RSS |
| college_higheredjobs, college_herc, college_academicjobsonline, college_cccregistry | aggregators | see module docstrings |
| college_academickeys | AcademicKeys (community-college / engineering / sciences sections) | `seeker_search.php` HTML + detail |
| college_theeduledger | TheEduLedger (formerly DiverseJobs / Diverse: Issues in Higher Education) | Playwright list (Altcha-guarded XHR) + JSON-LD detail |
| college_cra | CRA Career Center (Computing Research Association) | HTML search + JSON-LD detail |
| college_ymcareers | YourMembership career centers behind Cloudflare: ACM, IEEE (configs per board) | one Chromium load for cookies, then requests + JSON-LD |
| college_ccjobs | CommunityCollegeJobs.com | `search.php` HTML + detail |
| college_suny | SUNY system-wide employment search (64 campuses, links to campus ATSs) | ColdFusion POST + detail where reachable |
| college_sbctc | Washington SBCTC college-system job search (34 CTCs) | WebForms POST by discipline |
| college_ncccs | NC Community College System jobs (58 colleges) | `__NEXT_DATA__` (descriptions inline) |
| college_governmentjobs | GovernmentJobs.com + SchoolJobs.com national keyword search (every NeoGov agency) | XHR HTML fragment + JSON-LD detail |
| college_usajobs | USAJobs (federal academies, DoD schools, CCAF, DLI ...) | `/Search/ExecuteSearch` JSON + detail |
| college_schoolspringnational | SchoolSpring national search (adult-ed / career-center instructors) | `api.schoolspring.com` JSON |
| college_edjoin | EdJoin (California districts + community colleges) | `/Home/LoadJobs` JSON + JSON-LD detail |
| college_k12jobspot | K12JobSpot (AppliTrack districts) | `/api/job-postings` JSON |
| college_talent, college_adzuna | Talent.com and Adzuna job aggregators (keyword searches, US) | HTML cards + JSON-LD detail |

Workday, iCIMS, Cornerstone, UKG Pro, ADP, Paycom, Paylocity, Dayforce, Workable,
ApplicantPro, BambooHR, SmartRecruiters and Greenhouse boards found at colleges
reuse the existing fetchers in `scrape.py`; the entry's `slug` is the school's
name so the dashboard shows it. `fetch_adp` honours optional `ccId` and
`host_override` (e.g. `workforcenow.cloud.adp.com`) keys and `fetch_ultipro` an
optional `host` (`<tenant>.rec.pro.ukg.net`); the detectors fill them in from
the board URL / the `<recruitment-current-openings cid= ccid=>` web component.
Ellucian "applicant-apply" share links are admissions applications, not job
boards, and are deliberately not detected.

## Commands

```bash
python3 -m colleges.discover                   # crawl new / errored schools, rebuild colleges.json
python3 -m colleges.discover --redo-unclassified   # re-crawl schools with no classified board
python3 -m colleges.discover --redo-unclassified --browser --workers 12  # ...and retry in Chromium (hours)
python3 -m colleges.discover --inherit-only    # share boards across campuses of one system, rebuild
python3 -m colleges.discover --redo-errors --browser --workers 4   # retry schools whose crawl crashed
python3 -m colleges.merge_extras               # fold in data/extra_institutions.json, vendor_candidates.json, manual_boards_*.json
python3 -m colleges.discover --states FL GA --redo  # subset
python3 -m colleges.discover --build-only      # regenerate data/colleges.json only
python3 tools/scan_colleges.py --notify        # daily scan + report
python3 scrape.py --all                        # full scan, colleges included
```

The tab `adjunct_cs` (also a starter template) holds the title rules for
adjunct / instructor / lecturer roles in CS, IT, cybersecurity, software, data
and AI; tune it at `/tabs`. Full-time and tenure-track faculty titles are
listed only when the posting is taught online: the tab's
`exclude_unless_online` patterns (`\btenure`, `\bopen\s+rank\b`) are waived
when the title or location says online/remote/virtual or the description says
the courses are taught online (`scrape.job_is_online`). College fetchers mark
such postings `remote`, so the tab's Remote filter shows the online ones.
Generic teaching titles ("Adjunct Instructor", "Part-Time Faculty Pool",
"Adjunct Faculty - Business & Technology") carry no discipline, so the
fetchers keep them too (`scrape.generic_teaching_title`: teaching words plus
filler only) and `colleges.fetchers.register` wraps every fetcher with a
post-filter: such a posting survives only when `scrape.discipline_hit` finds
a computing discipline (computer science, information technology,
cybersecurity, software engineering, data science, MIS ...) in a teaching
context in the department or description ("Department: Computer Science",
"teach courses in ...", "disciplines: ..., Computer Information Systems").
Non-adjunct generic titles ("Lecturer", "Assistant Professor") use the strict
form (the discipline must be the posting's own unit/courses). A tab opts in
with `"domain_in_description": true` (adjunct_cs does); the matched sentence
becomes the row's snippet, prefixed with the phrase in brackets.
Every higher-ed posting also gets a role tag in the Stage column
(`scrape.classify_role`): **Adjunct** (adjunct, part-time, pool, temporary,
per-course ...), **Faculty** (professor, instructor, lecturer, tenure ... and
not flagged part-time in the description) or **Staff**; the Stage filter has
"College: Adjunct / Faculty / Staff" entries.
