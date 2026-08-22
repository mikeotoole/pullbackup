# Product name research: replacing "Pullback"

**Status:** preliminary knockout screen — research only, not a legal clearance opinion.
**Search date:** 2026-08-20 (all counts and registry states are as observed on that date).
**Jurisdiction:** United States federal records (USPTO) plus global developer/marketplace
namespaces. No EUIPO, WIPO, UKIPO, CIPO, or other national register was searched.
**Prepared for:** the `pullback` repository (pull-only rsync task manager, self-hosted,
Docker-deployed, web UI).

---

## 1. Executive conclusion

The current name **Pullback** should be replaced, and the reason is common-law and
namespace collision rather than federal trademark exposure. Two live software projects
occupy essentially the same product concept under essentially the same wording: an
active rsync-based pull backup system called **PullBackup**[2], and the GitHub repository
`sudaraka/pullback`, described verbatim as "Pull backup data from web server into local
server."[1] Both are in the identical field — pull-mode rsync backup — which is the single
worst place to share a mark. Federally, the picture is comparatively quiet: no live U.S.
registration for the exact wordmark PULLBACK exists; the two exact-wordmark records found
are both dead[4][5], and the only live containing record, ZERO PULLBACK, is in a ceramics
class unrelated to software[6]. The name is also semantically pre-owned by trading
vocabulary ("a pullback" is a price retracement), which harms search discoverability.

Three finalists survive the screen, in this order of preference:

1. **Kedgeport** — preferred. Zero hits in every channel checked, `.com/.io/.app/.dev` all
   unregistered at RDAP, and a semantically exact metaphor (kedging is hauling a vessel
   toward an anchor you have already placed).
2. **Sternhaul** — preferred. Also zero across every channel with all four domains
   unregistered; slightly weaker because "haul" is a crowded software morpheme.
3. **Haulport** — conditional. Clean in every code/app/trademark channel, but
   `haulport.com` has been registered since 2012 and currently serves a 114-byte
   placeholder, so the primary domain would have to be acquired or a different TLD used.

**Gantline** and **Hauldeck** are held in reserve. **PullKeep** is rejected late in the
screen: `pullkeep.com` currently serves a live application titled "Pullkeep."

No filing, purchase, reservation, domain registration, owner contact, or repository rename
was performed. Nothing in this note is legal advice, and it is not a clearance opinion.

---

## 2. Scope and methodology

### 2.1 What was screened

The product is downloadable/self-hostable server software with a web UI, distributed as a
container image, operated privately rather than sold as a hosted service. That places its
natural goods/services in **IC 009** (downloadable software) and **IC 042** (SaaS /
software design), so both classes were queried directly for every finalist. Class numbers
were used as triage only; the complete goods/services text of each material record was
read, because likelihood of confusion turns on related goods, not on class equality.

### 2.2 Channels and reproducible queries

Every candidate went through the same matrix. All endpoints are public and were used
read-only; no image was pulled, no package installed, no artifact executed.

| Channel | Query form | Interpretation rule used here |
|---|---|---|
| USPTO federal | `match_phrase` on `wordmark` against the live search backend of the official site[3], run twice — once filtered `alive:true`, once unfiltered | Returned wordmarks were normalized and compared, so **exact** counts are reported separately from **containing** counts |
| USPTO by class | Same query filtered to `IC 009` + `IC 042`, live only | Software-field conflict, distinct from any-class noise |
| npm | `GET https://registry.npmjs.org/<name>` | 404 = name free; 200 = taken, with published description |
| PyPI | `GET https://pypi.org/pypi/<name>/json` | Same convention |
| Docker Hub | `/v2/search/repositories/?query=<name>`, plus a direct `library/<name>` probe | Only repositories whose short name matched exactly were counted |
| GitHub | `<name> in:name` repository search via the authenticated API | Repository names compared case-insensitively for exact equality |
| Apple App Store | iTunes Search API, `entity=software&country=US` | Only exact display-name equality counted as a hit |
| Google Play | Public store search URL | Reachability check only — see limitations |
| Domains | RDAP for `.com`, `.io`, `.app`, `.dev`, plus a separate DNS A-record lookup | RDAP 404 = unregistered *at that moment*; DNS non-resolution alone was never treated as "available" |

Counts labelled "live" exclude dead records; counts labelled "all" include abandoned,
cancelled, and expired records. The two are never mixed in the tables below.

### 2.3 Candidate design

Twenty-three names were generated in a first round, drawn mainly from nautical hauling and
retrieval vocabulary (which fits "pull from a remote source into local storage" without
implying a hosted service or a particular backend). After the first round knocked out most
of them, a second round of eight coined compounds was generated and screened identically,
for thirty-one names in total. Candidates were assessed for spelling, pronunciation,
namespace availability, and confusion risk, not for legal cleanliness alone.

---

## 3. The incumbent: why "Pullback" fails

### 3.1 Federal records — comparatively quiet

An exact-phrase search on PULLBACK returned **7 records in total across live and dead
status, of which 2 were exact wordmarks and 0 of those exact records were live**; the live
subset of the phrase query contained a single record, and it is not an exact wordmark.[3]

| Mark | Serial | Reg. | Status | Owner | Class | Goods |
|---|---|---|---|---|---|---|
| PULLBACK | 90107888 | — | Abandoned — failure to respond[4] | Runske, Larry (individual, USA) | IC 025 | Fishing/hunting apparel |
| PULLBACK | 87699747 | 5840345 | Cancelled — Section 8[5] | Lucinei Nogueira Junior (individual, Brazil) | IC 025 | Clothing, footwear, headgear |
| ZERO PULLBACK | 77629146 | 3765597 | **Live** — registered and renewed[6] | Remtec, Inc. (Massachusetts) | IC 017 | Metalized-ceramic submounts for laser diodes |

The only live record containing the word is in optoelectronic ceramics, remote from backup
software; it is containing wording, not the exact mark. So the federal picture for PULLBACK
is not the problem.

### 3.2 Common-law and namespace — the actual problem

- **PullBackup** is an in-progress project described as "A backup system wrapped around
  rsync which pulls files from remote servers and incrementally backs them up," listed as
  active from May 2026 with 107 commits.[2] That is the same product category, the same
  transport, and the same pull semantics as this repository, under a mark that differs
  from "Pullback" by one syllable. This is the single strongest reason to rebrand.
- **`sudaraka/pullback`** is an exact repository-name match whose README reads "Pull backup
  data from web server into local server," BSD-2-Clause, last pushed 2018-05-19.[1] The
  repository is a real, if dormant, JavaScript codebase (src/, systemd/, package.json,
  CHANGELOG) rather than an empty name reservation — it was inspected, not inferred from
  metadata. Dormancy is not abandonment, and it does not clear the name.
- Four exact-name GitHub repositories carry the name overall, and `pullback.com` has been
  registered since 2000-02-25 with an expiry of 2027-02-25.[46] `.io`, `.app`, and `.dev`
  were unregistered at RDAP on the search date.
- The word already means something else to the target audience: "pullback" is standard
  trading vocabulary for a price retracement, which permanently dilutes search results.

npm and PyPI have no `pullback` package, and no exact Docker Hub repository was found — so
the package namespaces are free. The problem is product-field collision, not registries.

---

## 4. Screening matrix — full candidate set

Reading the columns: **npm/PyPI** = exact package name state; **Docker** = count of Docker
Hub repositories whose short name matched exactly; **GitHub** = count of exact
repository-name matches; **Domains** = which of `.com`/`.io`/`.app`/`.dev` were registered
at RDAP; **TM live** = live federal records returned by the exact-phrase query, with exact
wordmarks in parentheses; **TM all** = the same query unfiltered by status.

### Round 1 (23 names)

| # | Candidate | npm | PyPI | Docker | GitHub | Domains registered | TM live (exact) | TM all (exact) | Disposition |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Capstan | taken | taken | 0 | 13 | com, io | 8 (5) | 20 (13) | Reject |
| 2 | Kedge | taken | taken | 7 | 20 | com, io | 8 (5) | 16 (10) | Reject |
| 3 | Windlass | taken | taken | 4 | 20 | com, io | 8 (3) | 13 (5) | Reject |
| 4 | Inhaul | free | free | 0 | 1 | com | 0 (0) | 0 (0) | Reserve |
| 5 | Haulback | free | free | 0 | 1 | com, io | 1 (1) | 1 (1) | Reject |
| 6 | Gantline | free | free | 0 | 0 | com | 0 (0) | 0 (0) | Reserve |
| 7 | Sheave | taken | taken | 0 | 5 | com | 1 (0) | 11 (0) | Hold |
| 8 | Reeve | taken | taken | 1 | 5 | com, io | 18 (2) | 36 (2) | Reject |
| 9 | Halyard | taken | taken | 25 | 13 | com, io | 20 (3) | 34 (8) | Reject |
| 10 | Snatchblock | taken | free | 0 | 0 | com | 0 (0) | 0 (0) | Hold |
| 11 | Bollard | taken | taken | 0 | 19 | com, io | 13 (1) | 23 (3) | Reject |
| 12 | Warpline | taken | taken | 0 | 7 | com | 0 (0) | 0 (0) | Hold |
| 13 | Towline | taken | free | 0 | 3 | com | 1 (1) | 2 (2) | Reject |
| 14 | Longline | free | free | 0 | 17 | com, io | 1 (1) | 13 (8) | Hold |
| 15 | Creel | free | taken | 0 | 9 | com, io | 8 (1) | 18 (1) | Hold |
| 16 | Seine | taken | taken | 1 | 8 | com, io | 7 (0) | 31 (4) | Hold |
| 17 | Trawler | taken | taken | 5 | 20 | com, io | 6 (1) | 30 (2) | Reject |
| 18 | Dredge | taken | taken | 1 | 9 | com, io | 22 (0) | 63 (2) | Reject |
| 19 | Undertow | taken | free | 5 | 2 | com, io | 7 (5) | 29 (21) | Reject |
| 20 | Riptide | taken | taken | 6 | 14 | com, io | 68 (30) | 201 (100) | Reject |
| 21 | Siphon | taken | taken | 4 | 14 | com, io | 23 (1) | 64 (10) | Reject |
| 22 | Backhaul | taken | taken | 14 | 5 | com, io | 9 (0) | 21 (0) | Reject |
| 23 | Drawdown | free | free | 4 | 6 | com, io | 14 (3) | 31 (14) | Reject |

### Round 2 (8 coined compounds)

| # | Candidate | npm | PyPI | Docker | GitHub | Domains registered | TM live (exact) | TM all (exact) | Disposition |
|---|---|---|---|---|---|---|---|---|---|
| 24 | Kedgeport | free | free | 0 | 0 | none | 0 (0) | 0 (0) | **Preferred** |
| 25 | Sternhaul | free | free | 0 | 0 | none | 0 (0) | 0 (0) | **Preferred** |
| 26 | Haulport | free | free | 0 | 0 | com | 0 (0) | 0 (0) | **Conditional** |
| 27 | Hauldeck | free | free | 0 | 1 | com | 0 (0) | 0 (0) | Reserve |
| 28 | Haulkeep | free | free | 0 | 0 | com | 0 (0) | 0 (0) | Reserve |
| 29 | Haulyard | free | free | 0 | 0 | com | 0 (0) | 0 (0) | Reserve |
| 30 | Tugline | free | free | 0 | 0 | com | 0 (0) | 1 (1) | Reserve |
| 31 | PullKeep | free | free | 0 | 0 | com | 0 (0) | 0 (0) | Reject |

---

## 5. Rejections, with the strongest direct source

Each rejection below cites the specific record or repository that drives it, not a generic
search entry point.

**Capstan.** `cloudius-systems/capstan` is a live developer tool — "a tool for packaging
and running your application on OSv" — with 377 stars.[19] Both npm[32] and PyPI[37] are
taken by unrelated active software. Five live exact CAPSTAN registrations exist; the
software-relevant one is Celemony Software GmbH's CAPSTAN, serial 79104884, IC 009 for
music and sound editing software, live under a partial Section 71 & 15 acceptance.[9]
Same class as our goods.

**Kedge.** Two established Kubernetes-infrastructure projects use the exact name —
`kedgeproject/kedge` (298 stars, "Simple, Concise & Declarative Kubernetes
Applications")[20] and `improbable-eng/kedge` (257 stars, a Kubernetes edge proxy)[21] —
and seven exact Docker Hub repositories carry it.[42] Decisively, KEDGE SOFTWARE LLC has a
pending application, serial 99947632, in **IC 042** for SaaS software.[10] npm[72] and
PyPI[75] are both taken.

**Windlass.** Twenty exact GitHub repositories including two active infrastructure
projects,[26][27] a Docker Hub image with 12,593 pulls,[43] and a taken PyPI name whose
current occupant is an AI application framework.[39] The live WINDLASS registration (serial
87814168) is in knives and body armor,[11] so federal risk is low — this is a
crowding/discoverability rejection, not a trademark one. That distinction matters.

**Halyard.** The worst namespace crowding in the set: 25 exact Docker Hub repositories
including `armory/halyard` at 511,214 pulls,[41] Spinnaker's own `halyard` deployment tool
at 316 stars,[22] and a taken PyPI name.[38] The Avent/O&M Halyard registration (serial
86245581) is medical rather than software,[12] but the developer-ecosystem collision alone
is disqualifying for a devops tool.

**Bollard.** `fussybeaver/bollard` is *the* Rust Docker daemon API client, 1,354 stars,
last pushed 2026-08-18.[23] PyPI's `bollard` is "A Pythonic client for the Docker/Podman
Engine API." For a tool that ships as a container image, adopting the name of a widely used
Docker API client is a self-inflicted support burden. The live BOLLARD registration (serial
86194522) covers marine engines and IC 042 design services.[14]

**Undertow.** `undertow-io/undertow` is a 3,758-star web server.[24] Beyond that, Epic
Games' subsidiary holds a live IC 009 registration for UNDERTOW covering computer game
software (serial 77612882),[15] which is the same class as downloadable software. Twenty-one
exact wordmarks appear across live and dead records.

**Riptide.** Extremely crowded: 68 live records for the phrase, 30 of them exact wordmarks,
201 across all statuses. Directly on point, Crawford Technologies holds RIPTIDE reg.
4563793 in **IC 009** for "computer software for use in collecting, packaging and outputting
document content."[16] Add `RiptideNetworking/Riptide` at 1,285 stars.[76] Reject.

**Backhaul.** `Musixal/Backhaul` is an actively used 864-star networking tunnel,[25] and 14
exact Docker Hub repositories exist.[44] "Backhaul" is also ordinary network-infrastructure
vocabulary, which is exactly the pitfall of adopting protocol terminology.

**Drawdown.** Fourteen exact wordmarks across all statuses; the live IC 042 record belongs
to Project Drawdown Corporation for climate think-tank services (serial 88324405).[17] The
name is also finance vocabulary (as with "pullback") and `adamvleggett/drawdown` is a
109-star Markdown converter.[77] Poor product fit.

**Haulback.** A single federal record exists and it is the wrong one: HAULBACK, serial
99827026, filed by Mobius Holdings, LLC in **IC 042** for "artificial intelligence as a
service (AIAAS) services featuring software," status new application not yet assigned to an
examiner.[7] One live application in our own class, on a coined term, is a clean knockout.
It also stays too close to the incumbent name.

**Towline.** Ingram Barge Company holds TOWLINE reg. 4718463 in **IC 042** for
"non-downloadable, web-based software that provides tracking and location information for
transportation assets."[8] Live, renewed, software services. Reject.

**Reeve, Trawler, Dredge, Siphon, Seine, Creel, Longline, Sheave.** Rejected or held for a
mix of dictionary-word crowding and specific hits: the Cardano Foundation's REEVE
registration spans IC 009, 035, 036, and 042 for blockchain and data-processing software
(serial 79421885),[13] `t-matsudate/sheave` is an RTMP implementation in Rust,[78] and the
remainder show heavy exact-name reuse across GitHub and Docker Hub without offering better
product fit than the finalists.

**PullKeep — late rejection.** No trademark, package, or repository conflict was found, and
this name would otherwise have shortlisted. But `pullkeep.com` was registered on
2026-06-25[69] and currently serves a live single-page application whose page title and
Open Graph metadata both read "Pullkeep" (locale `ru_RU`). An occupied primary domain with
a live application under the exact name, in a project whose ownership and field could not be
determined from the page, is enough to drop it from the shortlist. Treat the site's purpose
as **uncertain** — this is a discovery finding, not a proven commercial trademark use.

---

## 6. Finalists

### 6.1 Kedgeport — preferred

*Pronunciation:* KEJ-port, two syllables, unambiguous. *Meaning:* kedging is warping a
vessel toward an anchor already laid ahead of it — the ship pulls itself to a fixed point,
which is precisely the pull-only architecture where the destination holds the credentials.

| Channel | Finding |
|---|---|
| USPTO exact, live and all | 0 records, both filtered and unfiltered[3] |
| USPTO IC 009 + IC 042, live | 0 records[3] |
| npm | not found (404) |
| PyPI | not found (404) |
| Docker Hub | 0 exact repositories |
| GitHub | 0 exact repository names, 0 name hits total |
| Apple App Store | 19 fuzzy results, 0 exact display-name matches[60] |
| Domains | `.com`[52], `.io`, `.app`[70], `.dev` all returned RDAP not-found |

*Strongest surviving caveat:* the dominant element KEDGE is itself crowded — five live exact
KEDGE registrations exist, including a pending IC 042 SaaS application by KEDGE SOFTWARE
LLC.[10] A compound is not automatically clear of its dominant term, and a
one-letter-visible difference in a search index is not the same as a legal distinction. This
is the single question most worth putting to counsel. Nobody currently uses "Kedgeport" in
any channel checked; that is a favourable starting position, not clearance.

### 6.2 Sternhaul — preferred

*Pronunciation:* STERN-hawl, transparent to English speakers, low misspelling risk. *Meaning:*
hauling from the stern; retrieval toward the operator.

| Channel | Finding |
|---|---|
| USPTO exact, live and all | 0 records[3] |
| USPTO IC 009 + IC 042, live | 0 records[3] |
| npm / PyPI | not found (404) |
| Docker Hub | 0 exact repositories[80] |
| GitHub | 0 exact repository names |
| Apple App Store | 4 fuzzy results, 0 exact display-name matches[58] |
| Domains | `.com`[51], `.io`, `.app`[55], `.dev` all RDAP not-found |

*Strongest surviving caveat:* "haul" is a crowded morpheme in both software and logistics.
Inhauler, a freight-logistics technology company, operates in an adjacent
software-plus-transport space,[66] and HAULBACK is pending in IC 042 for AI software.[7]
Neither is a conflict with "Sternhaul," but the family is busy enough that a broad
registration on the HAUL- element should not be assumed obtainable. Also note the word
appears as ordinary sailing vocabulary in dinghy descriptions, which slightly weakens
distinctiveness arguments while helping nothing commercially.

### 6.3 Haulport — conditional

*Pronunciation:* HAWL-port, unambiguous. *Meaning:* the port that hauls; also reads as a
network port, which is apt for a server tool.

| Channel | Finding |
|---|---|
| USPTO exact, live and all | 0 records[3] |
| USPTO IC 009 + IC 042, live | 0 records[3] |
| npm / PyPI | not found (404) |
| Docker Hub | 0 exact repositories |
| GitHub | 0 exact repository names |
| Apple App Store | 0 exact display-name matches |
| Domains | `haulport.com` **registered** 2012-06-20, expiry 2027-06-20[68]; `.io`, `.app`, `.dev` RDAP not-found |

*Strongest surviving caveat:* `haulport.com` resolves and returns HTTP 200, but the response
is a 114-byte document with no title and no visible text — consistent with a parked or
placeholder page. Non-resolution and thin content are **not** proof of abandonment, and
registration since 2012 says nothing about trademark rights either way. Adopting Haulport
means either operating on `.app`/`.io` or negotiating for the `.com`, and negotiation is
outside this task's scope. The "haul" crowding caveat from Sternhaul applies here too.

### 6.4 Reserve

**Inhaul** (the line that hauls a sail inboard) is clean in every package registry and
returned no federal records at all, but it is held in reserve rather than advanced as a
finalist: Inhauler, a freight-logistics technology company, is a common-law sound-alike in
an adjacent industry,[66] and that proximity is the kind of confusion risk this screen is
meant to avoid. Reconsider only if counsel judges the logistics adjacency immaterial.

**Gantline** (a rope rove through a block for hoisting) is clean federally and in every
package registry, but `gantline.com` is registered and serves a JavaScript redirect to a
`/lander` path — a domain-parking pattern. Two dissolved UK companies used the name,
including GANTLINE LTD (company 10693818, dissolved 2026-06-09),[67] and a GitHub user
account `@Gantline` exists. None of these is a live software conflict; the name is a valid
fallback if both preferred names fail counsel review. **Hauldeck** and **Haulkeep** are
similarly clean in federal and package channels, with `.com` registered recently
(2026-07-09[50] and 2025-12-24[49] respectively) and not currently resolving to any A
record; `BjornB2/HaulDeck` is a zero-star repository[30] with no described product.

---

## 7. Risk assessment by intended use

Adoption/confusion risk and registrability/examination risk are separate axes, not levels of
one scale. A name can be easy to adopt and hard to register, or the reverse.

| Intended use | Kedgeport | Sternhaul | Haulport | Keeping Pullback |
|---|---|---|---|---|
| Repository / package / image name | Low | Low | Low | **High** — exact repo collision[1] |
| Public project name in the rsync-backup field | Low | Low | Low | **High** — PullBackup occupies the field[2] |
| Customer-facing SaaS offering | Low–moderate (KEDGE crowded in IC 042[10]) | Low–moderate | Low–moderate | High |
| Product name under a separate house brand | Low | Low | Low | Moderate |
| Broad federal registration in IC 009 + IC 042 | Moderate — dominant-term crowding | Moderate — HAUL- family crowding | Moderate | Not assessed; poor prospects given the field |
| Primary `.com` acquisition | Low — unregistered[52] | Low — unregistered[51] | **Blocked** — registered since 2012[68] |  Registered since 2000[46] |

Low / moderate / high are preliminary research labels reflecting evidence and uncertainty,
not legal conclusions.

---

## 8. Recommendation

Adopt **Kedgeport** for the repository, package, and image name, with **Sternhaul** as the
immediate alternate if counsel dislikes the KEDGE dominant-term crowding. Both leave every
namespace and all four candidate domains open on the search date, which is the practical
requirement for a rename that touches a repository, an npm package, a PyPI package, a
container image, and a public URL simultaneously.

Do not treat this note as authorization to rename anything. The rename itself, domain
acquisition, and any filing are explicitly out of scope here.

---

## 9. Questions for trademark counsel

1. Does the pending KEDGE SaaS application in IC 042 (serial 99947632)[10] create a
   meaningful objection to KEDGEPORT in the same class, or does the compound create a
   distinct commercial impression?
2. Is HAUL- crowded enough in IC 009/IC 042 that a broad registration on STERNHAUL would be
   narrowed at examination, and does the pending HAULBACK AIAAS application[7] matter?
3. Does dormant-but-published open-source use — `sudaraka/pullback`, last pushed 2018[1] —
   support any common-law priority a rebrand should still respect?
4. Should the rebrand seek an IC 009 filing, an IC 042 filing, or both, given the software is
   self-hosted and distributed rather than sold as a hosted service?
5. Is a Supplemental Register filing worth considering for any candidate, and would that
   change the adoption calculus?
6. What non-U.S. registers should be searched before any public launch, given the software
   is distributed globally through Docker Hub and GitHub?

---

## 10. Limitations and disclaimers

- **This is a preliminary knockout screen, not a legal clearance opinion.** It was performed
  by an automated research process without attorney review. Do not rely on it for a launch,
  filing, or investment decision.
- **Jurisdiction is U.S. federal only.** No state trademark register, no EUIPO, WIPO, UKIPO,
  or other national register was searched. Common-law rights can exist without any
  registration anywhere.
- **Records change.** Every USPTO status here should be re-checked in TSDR immediately before
  any decision. Search-index results are good for discovery; TSDR is the better citation for
  record detail. Note that some TSDR status URLs returned HTTP 403 to a scripted fetch on the
  search date while others returned 200 — the same URLs load normally in a browser, so the
  links are provided for human verification rather than as machine-fetched evidence.
- **Domain findings are point-in-time.** An RDAP not-found result is favourable availability
  evidence at that moment; it is neither a reservation nor a trademark right. DNS
  non-resolution was never treated as availability.
- **Google Play was checked only for reachability**, not parsed for exact app-name matches;
  its results are rendered client-side and the public search URL does not expose a stable
  machine-readable exact-match field. Treat Play as an unscreened channel for these
  candidates.
- **Search-engine absence is not proof of absence of common-law use.** Sites described here
  as parked, placeholder, or of uncertain purpose are labelled as uncertain deliberately.
- **No external action was taken.** No trademark application was filed, no domain was
  purchased or reserved, no owner was contacted, no counsel was engaged, no package was
  installed, and no candidate container image was pulled, run, or unpacked.

---

## Sources

[1] https://github.com/sudaraka/pullback — sudaraka/pullback — Pull backup data from web server into local server (GitHub)
[2] https://nictitate.net — nictitate.net project index - PullBackup, rsync pull backup system
[3] https://tmsearch.uspto.gov — USPTO Trademark Search (official search UI)
[4] https://tsdr.uspto.gov/statusview/sn90107888 — TSDR status - PULLBACK, serial 90107888
[5] https://tsdr.uspto.gov/statusview/sn87699747 — TSDR status - PULLBACK, serial 87699747
[6] https://tsdr.uspto.gov/statusview/sn77629146 — TSDR status - ZERO PULLBACK, serial 77629146
[7] https://tsdr.uspto.gov/statusview/sn99827026 — TSDR status - HAULBACK, serial 99827026
[8] https://tsdr.uspto.gov/statusview/sn76716350 — TSDR status - TOWLINE, serial 76716350
[9] https://tsdr.uspto.gov/statusview/sn79104884 — TSDR status - CAPSTAN, serial 79104884
[10] https://tsdr.uspto.gov/statusview/sn99947632 — TSDR status - KEDGE, serial 99947632
[11] https://tsdr.uspto.gov/statusview/sn87814168 — TSDR status - WINDLASS, serial 87814168
[12] https://tsdr.uspto.gov/statusview/sn86245581 — TSDR status - HALYARD, serial 86245581
[13] https://tsdr.uspto.gov/statusview/sn79421885 — TSDR status - REEVE, serial 79421885
[14] https://tsdr.uspto.gov/statusview/sn86194522 — TSDR status - BOLLARD, serial 86194522
[15] https://tsdr.uspto.gov/statusview/sn77612882 — TSDR status - UNDERTOW, serial 77612882
[16] https://tsdr.uspto.gov/statusview/sn86128241 — TSDR status - RIPTIDE, serial 86128241
[17] https://tsdr.uspto.gov/statusview/sn88324405 — TSDR status - DRAWDOWN, serial 88324405
[19] https://github.com/cloudius-systems/capstan — cloudius-systems/capstan - packaging/running apps on OSv
[20] https://github.com/kedgeproject/kedge — kedgeproject/kedge - declarative Kubernetes applications
[21] https://github.com/improbable-eng/kedge — improbable-eng/kedge - Kubernetes edge proxy
[22] https://github.com/spinnaker/halyard — spinnaker/halyard - configuring, installing and updating Spinnaker
[23] https://github.com/fussybeaver/bollard — fussybeaver/bollard - Docker daemon API in Rust
[24] https://github.com/undertow-io/undertow — undertow-io/undertow - high performance non-blocking webserver
[25] https://github.com/Musixal/Backhaul — Musixal/Backhaul - reverse tunneling solution for NAT traversal
[26] https://github.com/Annex-Engineering/windlass — Annex-Engineering/windlass - Rust Klipper host protocol
[27] https://github.com/UCCNetsoc/Windlass — UCCNetsoc/Windlass - self-hosted containers-as-a-service platform
[30] https://github.com/BjornB2/HaulDeck — BjornB2/HaulDeck - GitHub repository
[32] https://registry.npmjs.org/capstan — npm registry record - capstan
[37] https://pypi.org/pypi/capstan/json — PyPI JSON record - capstan
[38] https://pypi.org/pypi/halyard/json — PyPI JSON record - halyard
[39] https://pypi.org/pypi/windlass/json — PyPI JSON record - windlass
[41] https://hub.docker.com/v2/search/repositories/?query=halyard&page_size=25 — Docker Hub repository search - halyard
[42] https://hub.docker.com/v2/search/repositories/?query=kedge&page_size=25 — Docker Hub repository search - kedge
[43] https://hub.docker.com/v2/search/repositories/?query=windlass&page_size=25 — Docker Hub repository search - windlass
[44] https://hub.docker.com/v2/search/repositories/?query=backhaul&page_size=25 — Docker Hub repository search - backhaul
[46] https://rdap.verisign.com/com/v1/domain/pullback.com — RDAP record - pullback.com
[49] https://rdap.verisign.com/com/v1/domain/haulkeep.com — RDAP record - haulkeep.com
[50] https://rdap.verisign.com/com/v1/domain/hauldeck.com — RDAP record - hauldeck.com
[51] https://rdap.verisign.com/com/v1/domain/sternhaul.com — RDAP record - sternhaul.com
[52] https://rdap.verisign.com/com/v1/domain/kedgeport.com — RDAP record - kedgeport.com
[55] https://www.registry.google/rdap/domain/sternhaul.app — RDAP record - sternhaul.app
[58] https://itunes.apple.com/search?term=sternhaul&entity=software&country=US&limit=50 — Apple App Store search API - sternhaul
[60] https://itunes.apple.com/search?term=kedgeport&entity=software&country=US&limit=50 — Apple App Store search API - kedgeport
[66] https://inhauler.com/how-inhauler-uses-technology-to-support-direct-shippers — Inhauler — logistics technology company (common-law sound-alike)
[67] https://find-and-update.company-information.service.gov.uk/company/10693818 — GANTLINE LTD (dissolved) — UK Companies House
[68] https://rdap.verisign.com/com/v1/domain/haulport.com — RDAP record - haulport.com
[69] https://rdap.verisign.com/com/v1/domain/pullkeep.com — RDAP record - pullkeep.com
[70] https://www.registry.google/rdap/domain/kedgeport.app — RDAP record - kedgeport.app
[72] https://registry.npmjs.org/kedge — npm registry record - kedge
[75] https://pypi.org/pypi/kedge/json — PyPI JSON record - kedge
[76] https://github.com/RiptideNetworking/Riptide — RiptideNetworking/Riptide - C# networking solution
[77] https://github.com/adamvleggett/drawdown — adamvleggett/drawdown - Markdown to HTML in JavaScript
[78] https://github.com/t-matsudate/sheave — t-matsudate/sheave - RTMP client/server in Rust
[80] https://hub.docker.com/v2/search/repositories/?query=sternhaul&page_size=25 — Docker Hub repository search - sternhaul
