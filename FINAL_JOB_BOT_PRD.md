# Final PRD & Implementation Blueprint: LinkedIn + Company Careers Job Application Bot

## 1. Purpose
Build a local-first, implementation-grade job application platform focused on:

- LinkedIn job discovery and application assistance
- Individual company career pages
- ATS-backed company sites, especially Workday, Greenhouse, and Lever

The system must optimize for:

- Reliability over flashy autonomy
- High signal job matching
- Fact-preserving resume and answer tailoring
- Controlled browser automation
- Full auditability and replay

The system is not a blind mass-apply bot. It is a constrained automation platform with deterministic workflows first and LLM assistance only where ambiguity justifies it.

## 2. Product Positioning
This bot should automate the expensive parts of job hunting:

- finding relevant jobs
- deduplicating and tracking them
- extracting structured requirements
- scoring fit
- tailoring application materials
- pre-filling repetitive application fields
- executing applications on stable flows

It should not fully trust autonomous decision-making on unknown sites or low-confidence forms.

Primary modes:

- Draft mode: prepare everything, stop before submit
- Guarded submit mode: auto-submit only on high-confidence flows
- Assist mode: user drives, bot suggests and fills

## 3. Design Principles

### 3.1 Deterministic First
Use deterministic extraction, mapping, and browser control wherever possible. Use LLMs to resolve ambiguity, not as the default control plane.

### 3.2 Fact Preservation: Three-Tier Truth Model

All generated content must be grounded in structured candidate facts plus approved source resume text. The system uses a four-level truth classification that governs what can be generated, how it is labeled, and whether it requires human approval.

#### Tier 1: Observed Facts
Directly evidenced in source resume, profile, or employment records.

- employment dates, company names, degrees, certifications, shipped features
- confidence: 1.0
- never modified in substance — only reformatted for tone or vocabulary
- never requires human review for approval

#### Tier 2: Supported Inferences
Derivable from Tier 1 facts with explicit reasoning.

- "Used React" inferred from "built React dashboards for sales team"
- "Collaborated cross-functionally" inferred from bullet mentioning coordination with design and engineering
- confidence: 0.85–0.99
- user should review on first use; approved inferences are cached for reuse
- labeled internally as `inference: true` for audit trail

#### Tier 3: Plausible Extensions (User-Consented)
Not directly evidenced but defensible from adjacent experience. May be generated only when ALL of the following constraints are satisfied:

- must be easily learnable or plausibly already known from adjacent work
- must be something the candidate could discuss competently in an interview after brief preparation
- must not claim scale, leadership, or ownership not supported by Tier 1 evidence
- must not claim credentials from specific institutions not attended
- must be tagged as `extension: true` in the artifact record with provenance links to supporting Tier 1 facts
- must carry a derived confidence of 0.60–0.84
- **always routed to review queue** — never auto-approved for submission
- user can revoke any approved extension, which removes it from all future applications without re-tailoring unrelated content
- when approved, the system must generate "interview prep notes" so the candidate can defend the claim if asked

Examples of allowed Tier 3 extensions:

- candidate worked at a data company and built internal tools → "Built data processing scripts and internal reporting tools using Python and SQL" (Tier 3 — plausible domain exposure)
- candidate worked on a team that deployed with Docker → "Familiar with containerized deployment workflows using Docker" (Tier 3 — adjacent exposure, not ownership claim)
- candidate used Jira on a project → "Experience working within Agile/Scrum tooling ecosystems" (Tier 3 — tool-adjacent framing)

#### Tier 4: Forbidden Fabrications
Verifiably false and indefensible under any circumstances.

- fake degrees, fake employers, fake dates, fake scale metrics
- "Led architecture for a distributed platform serving millions of users" when the candidate has no production ownership evidence
- confidence: N/A — never generated, never proposed

The system must explicitly label every generated claim with its truth tier:

```json
{
  "bullet": "Developed React and TypeScript dashboards for business stakeholders",
  "tier": 2,
  "source_facts": ["fact_id_42"],
  "inference_reason": "candidate built dashboards; React/TypeScript confirmed in skills list"
}
```

```json
{
  "bullet": "Familiar with containerized deployment workflows using Docker",
  "tier": 3,
  "source_facts": ["fact_id_17", "fact_id_23"],
  "extension_reason": "candidate worked on deployable services; team used Docker; plausible exposure",
  "interview_prep": "Review Dockerfile basics, multi-stage builds, and docker-compose patterns"
}
```

This product maximizes conversion by improving relevance, presentation, and strategic emphasis — not by inventing achievements the candidate cannot defend in interview or employment verification.

### 3.3 Idempotent Pipeline
Each stage must be restartable without redoing unrelated work. A failed apply attempt must not require rediscovery, re-enrichment, or unnecessary re-tailoring.

### 3.4 Observable by Default
Every significant step must be logged with enough data to explain:

- what happened
- why it happened
- what the model saw
- what the browser did
- why a submit was or was not allowed

### 3.5 Human Approval for Low Confidence
The system must stop and request review whenever field mapping, answer generation, or submit readiness falls below configured thresholds.

## 4. Scope

### 4.1 In Scope

- LinkedIn discovery and application assistance
- Company careers pages
- ATS-backed sites: Workday, Greenhouse, Lever
- Resume tailoring
- Cover letter generation
- Screening question answering
- Application tracking
- Manual review queue
- Browser automation with persistent sessions

### 4.2 Out of Scope for Initial Versions

- Broad multi-board scraping beyond LinkedIn and company sites
- CAPTCHA bypass as a core feature
- Mobile app
- Multi-user SaaS deployment
- Full recruiter CRM

## 5. User Stories

1. As a candidate, I want to discover relevant jobs from LinkedIn and company sites without re-reviewing duplicates.
2. As a candidate, I want each job converted into a structured profile with extracted requirements and a fit score.
3. As a candidate, I want the system to tailor my resume and draft answers, reframing my real experience in the target role's vocabulary.
4. As a candidate, I want the system to propose plausible skill-adjacent bullets for my review when my profile has gaps relative to the job, clearly labeled as extensions I must approve.
5. As a candidate, I want the bot to apply automatically only when confidence is high and all content is Tier 1 or Tier 2 approved.
6. As a candidate, I want unclear jobs, Tier 3 extensions, and ambiguous forms routed to me for approval.
7. As a candidate, I want interview prep notes generated for any Tier 3 extension I approve so I can defend every claim.
8. As a candidate, I want a full history of what was submitted and why, including the truth tier of every generated claim.

## 6. System Architecture

### 6.1 Major Components

- `discovery`: LinkedIn and company/ATS job intake
- `normalization`: canonical job schema
- `deduplication`: URL and content-level duplicate detection
- `enrichment`: structured extraction of job requirements
- `scoring`: fit analysis against candidate profile
- `tailoring`: resume, cover letter, and answer generation
- `execution`: browser automation and guarded submission
- `review`: human approval queue
- `tracking`: persistence, artifacts, analytics
- `ui`: local dashboard and workflow control

### 6.2 Runtime Topology

- Python backend process
- local database
- browser worker pool
- file artifact store
- local dashboard server

Optional later:

- background queue worker process
- remote model router

## 7. Recommended Technical Stack

### 7.1 Backend

- Python 3.12+
- FastAPI
- Pydantic v2
- SQLAlchemy 2.x
- Alembic
- Typer
- Rich

### 7.2 Browser Automation

- Playwright Python
- real Chrome/Edge channel
- persistent profile directories
- trace capture

Do not depend on a proprietary CLI agent for the core browser execution path.

### 7.3 Data

- SQLite in WAL mode for local-first single-user operation
- Postgres support as a later migration path

### 7.4 AI and NLP

- internal provider abstraction layer
- OpenAI-compatible support
- Gemini support
- optional local models via Ollama or similar
- spaCy and rule-based parsers for low-cost extraction and normalization

### 7.5 Frontend

- server-rendered FastAPI templates
- HTMX for interactivity
- Tailwind CSS if desired, but UI choices are secondary to workflow clarity

## 8. Candidate Knowledge Model
Do not model the user as a single resume file.

Represent the candidate as structured data:

- personal details
- contact links
- work authorization
- target titles
- target locations
- remote/hybrid preferences
- compensation bounds
- employment history
- achievements
- skills
- technologies
- education
- certifications
- portfolio/work samples
- writing style preferences
- banned claims

Inputs:

- one or more source resumes
- manual edits from user
- imported LinkedIn profile data if available

Outputs derived from this model:

- tailored resume variants
- summary rewrites
- cover letter snippets
- screening answers
- recruiter messages

## 9. Canonical Job Schema
Every discovered job should normalize to a common schema:

- `source`
- `source_type`
- `external_job_id`
- `canonical_url`
- `company_name`
- `company_domain`
- `title`
- `location_raw`
- `location_normalized`
- `remote_type`
- `employment_type`
- `seniority`
- `salary_text`
- `salary_min`
- `salary_max`
- `currency`
- `description_raw`
- `description_text`
- `requirements_structured`
- `benefits_structured`
- `application_url`
- `ats_vendor`
- `discovered_at`
- `last_seen_at`
- `status`

## 10. Pipeline

### Stage 1: Discover
Sources:

- LinkedIn search result pages and saved searches
- direct company career pages
- ATS listing pages for Workday, Greenhouse, Lever

Responsibilities:

- collect listing URLs
- capture metadata available at listing level
- canonicalize URLs
- enqueue new jobs

Notes:

- prioritize company pages over third-party aggregators
- maintain source-specific adapters
- support incremental rescans

### Stage 2: Normalize and Deduplicate

Deduplicate using:

- canonical URL
- ATS job id
- company + title + location fingerprint
- fuzzy title similarity within time window

Keep provenance:

- same job may be seen from LinkedIn and direct company page

### Stage 3: Enrich
Use a tiered cascade:

1. structured data extraction
   - JSON-LD
   - known ATS APIs or embedded JSON
2. deterministic extractor
   - source-specific selectors and parsers
3. LLM extraction fallback
   - only on pages that remain insufficiently structured

Extract:

- responsibilities
- must-have skills
- nice-to-have skills
- years of experience
- education requirements
- domain hints
- travel/onsite requirements
- visa language
- compensation hints

### Stage 4: Score
Compute multiple scores, not one opaque score:

- qualification fit
- location fit
- compensation fit
- work authorization fit
- seniority fit
- resume coverage score
- confidence score

Output:

- overall recommendation
- reasons
- blocking mismatches
- suggested resume variant

### Stage 5: Tailor
Generate:

- tailored resume variant
- optional cover letter
- reusable screening answers
- recruiter note if requested
- interview prep notes for any Tier 3 extensions

Rules by truth tier:

**Tier 1 (Observed Facts):**
- retrieve the most relevant existing facts, projects, bullets, and skills from the candidate knowledge model
- rewrite those facts in the vocabulary of the target job
- prefer bullet selection and reordering over uncontrolled rewriting

**Tier 2 (Supported Inferences):**
- generate inferences only from combinations of already approved Tier 1 facts
- every inference must carry a reference to its source facts
- route to review on first use; cache approved inferences for reuse

**Tier 3 (Plausible Extensions):**
- identify uncovered job requirements and classify them as:
  - directly supported (Tier 1)
  - partially supported (Tier 2 inference possible)
  - unsupported but adjacent (Tier 3 extension candidate)
  - unsupported and non-adjacent (Tier 4 — cannot close this gap)
- for Tier 3 candidates, generate a plausible extension bullet with:
  - provenance links to supporting Tier 1 facts
  - a confidence score (0.60–0.84)
  - interview prep notes for defensive preparation
  - clear labeling as an extension in the review queue
- **never auto-approve Tier 3 content** — always route to review queue

**Tier 4 (Forbidden Fabrications):**
- never generate, never propose
- if a job requirement falls into Tier 4, the system must either:
  - omit the requirement from the tailored resume
  - mark it as a candidate gap in internal scoring
  - generate a truthful bridging statement (transferable skill framing)
  - route the job to review with an explicit gap warning

Examples of allowed transformation:

- from factual source: `built internal dashboards in React and TypeScript used by sales ops`
- to tailored bullet: `Developed React and TypeScript dashboards for business stakeholders, improving internal reporting workflows for sales operations`

Examples of allowed Tier 3 extension:

- candidate worked at a fintech company on internal tools → "Built financial data processing and reporting tools within a regulated fintech environment" (plausible domain framing)
- candidate's team deployed with Kubernetes → "Familiar with container orchestration concepts and Kubernetes deployment workflows" (adjacent exposure, not ownership)

Examples of forbidden transformation:

- source has no production ownership evidence
- generated bullet claims `Led architecture for a multi-region distributed platform serving millions of users`

The tailoring engine must optimize for:

- truthfulness (every claim traceable to a tier and source fact)
- relevance (prioritize bullets and skills that match the target job)
- specificity (use concrete metrics, technologies, and outcomes from Tier 1)
- interview defensibility (candidate can explain and defend every bullet)
- consistency across resume, answers, and cover letter (same tier labels, same facts)

### Stage 6: Prepare Application

Before browser execution:

- detect apply target
- detect login readiness
- check required artifacts exist
- precompute answer pack
- assign confidence for automation

### Stage 7: Execute Application

Use deterministic browser flows first:

- known ATS handlers
- known widget handlers
- known upload patterns
- known next/review/submit patterns

Use LLM assistance only for:

- semantic field label interpretation
- ambiguous question understanding
- novel layout reasoning

Guard rails:

- no final submit on low confidence
- screenshot before submit
- user-configurable submit confirmation

### Stage 8: Track and Learn

Persist:

- application status
- trace artifacts
- answers used
- generated documents used
- failure reasons
- manual interventions

Feed outcomes back into:

- scoring
- answer reuse
- preferred resume variant selection

## 11. Browser Automation Strategy

### 11.1 Session Management

- use persistent browser profiles
- allow user-seeded authenticated LinkedIn/company sessions
- clone profiles for workers only when needed
- keep worker count low and configurable

Session lifecycle requirements:

- detect logged-out, expired, checkpointed, and rate-limited sessions before application execution
- maintain per-profile health status such as `healthy`, `login_required`, `checkpointed`, `rate_limited`, `suspected_flagged`
- require explicit re-auth workflow when session health is not `healthy`
- stop high-risk automation on profiles that show repeated redirects, forced re-login, unusual challenge pages, or degraded search/application visibility
- separate discovery profiles from application profiles if account risk or session volatility requires it

CDP guidance:

- CDP is not the centerpiece of the product narrative
- CDP is, however, a first-class implementation tool for attaching to existing authenticated Chrome sessions and preserving stateful browsing workflows
- prefer persistent profiles first, then use Playwright support for connecting to those real browser sessions where that improves session reuse and operator control

### 11.2 Worker Model

- one worker per browser context or profile
- per-worker event log
- per-worker artifact directory
- strict cleanup and timeout handling

### 11.3 Apply Strategy by Site Type

#### LinkedIn

- support job discovery first
- support Easy Apply assistance and guarded execution
- do not assume all LinkedIn flows are safe for blind submission
- treat LinkedIn as high-value but high-risk for detection and layout drift

#### Greenhouse

- likely strongest early automation target
- forms are relatively structured
- implement deterministic field handlers early

#### Lever

- similar priority to Greenhouse
- usually straightforward application pages

#### Workday

- high-value but more variable
- build site family support carefully
- expect custom widgets and inconsistent flows

#### Custom Company Pages

- detect whether page is custom front-end over ATS backend or fully custom
- route unknown flows to assist mode until extractor confidence improves

## 12. Field Mapping Model
Every field interaction should resolve to a typed answer plan.

Field types:

- full name
- first name
- last name
- email
- phone
- location
- LinkedIn URL
- portfolio URL
- work authorization
- sponsorship need
- resume upload
- cover letter upload
- salary expectation
- start date
- years of experience
- free-text screening answer
- single select
- multi select
- checkbox acknowledgement
- demographic/compliance

For each field store:

- field label text
- field HTML characteristics
- inferred canonical field type
- confidence
- chosen answer
- answer source

## 13. AI Usage Policy

### 13.1 Allowed Uses

- job requirement extraction fallback
- fit analysis
- answer drafting
- field label interpretation
- cover letter drafting
- semantic matching between question and approved fact set

### 13.2 Disallowed Uses (Tier 4 — Forbidden Fabrications)

- inventing credentials (degrees, certifications, licenses not held)
- inventing dates or employers (employment periods or companies not in Tier 1)
- inventing years of experience beyond what Tier 1 evidence supports
- claiming production, leadership, scale, domain, or ownership experience not evidenced in approved candidate facts
- autonomous final submit when confidence is below threshold
- generating any Tier 4 content under any circumstances

The following are **allowed** under their respective tiers:

- **Tier 1**: verbatim or reformatted facts from source resume/profile — always allowed
- **Tier 2**: inferences derivable from Tier 1 with explicit reasoning — allowed with first-use review
- **Tier 3**: plausible extensions from adjacent experience — allowed only with explicit user review and approval, never auto-submitted

### 13.4 Gap Handling Policy

When a job description contains requirements not directly supported by candidate facts, the system must classify each gap by tier:

- **directly supported** → Tier 1: use factual evidence as-is
- **partially supported** → Tier 2: generate inference with explicit reasoning
- **adjacent but unsupported** → Tier 3: propose plausible extension for user review
- **non-adjacent and unsupported** → Tier 4: cannot close this gap

Allowed gap-closing mechanisms:

- emphasize adjacent technologies or analogous project work (Tier 2)
- rewrite a genuine achievement using terminology closer to the target stack (Tier 1 reframe)
- generate a plausible extension bullet from adjacent experience (Tier 3 — requires review and approval)
- generate a truthful learning or exposure statement for use in optional answers, if the user enables that style (Tier 3)
- lower fit score while still allowing application if the overall profile remains attractive

Forbidden gap-closing mechanisms (Tier 4):

- creating tools, projects, metrics, or impact that never occurred
- upgrading support work into ownership without evidence
- upgrading experimentation into production expertise without evidence
- converting familiarity into years of hands-on experience
- inventing credentials, employers, dates, or scale metrics

Implementation requirements:

- every generated bullet must be tagged with its truth tier and retain links to one or more supporting source facts
- Tier 3 bullets must include an `extension` flag, confidence score, interview prep notes, and provenance chain
- if no supporting fact exists for any tier (including Tier 3 adjacency), the bullet is invalid and must be rejected before document generation
- Tier 3 bullets are never included in auto-submitted applications; they always route to the review queue

### 13.3 Model Routing

- cheap model for extraction/classification
- stronger model for tailoring and difficult questions
- strongest model only for rare ambiguous browser reasoning

The implementation should support provider swapping without changing business logic.

## 14. Confidence and Safety Gates
Every important automation decision must produce a confidence value.

Examples:

- extractor confidence
- field mapping confidence
- answer confidence
- submit readiness confidence
- truth-tier confidence (per generated claim)

Suggested policy:

- high confidence: proceed automatically
- medium confidence: proceed to review page, do not submit
- low confidence: pause and require manual intervention

Truth-tier confidence policy:

- Tier 1 (Observed Facts): confidence 1.0 — no review required, auto-eligible
- Tier 2 (Supported Inferences): confidence 0.85–0.99 — review on first use, cache after approval
- Tier 3 (Plausible Extensions): confidence 0.60–0.84 — always routed to review, never auto-submitted
- Tier 4 (Forbidden): never generated

Submit readiness tier rule:

- auto-submit is blocked if any generated content in the application pack contains Tier 3 extensions that have not been explicitly approved by the user
- auto-submit requires all content to be Tier 1 or Tier 2-approved, plus submit readiness confidence ≥ threshold

Initial calibration policy:

- use numeric confidence values in the range `0.0` to `1.0`
- start with conservative defaults:
  - extractor auto-accept threshold: `0.90`
  - field mapping auto-fill threshold: `0.92`
  - free-text answer auto-use threshold: `0.88`
  - submit readiness auto-submit threshold: `0.95`
  - Tier 3 extension minimum proposal threshold: `0.60` (below this, do not even propose)
- any required field below threshold blocks auto-submit
- medium-confidence band should route to review rather than fail closed where practical

Calibration requirements:

- record predicted confidence and actual outcome for every important decision
- support later threshold tuning based on false positive and false negative rates
- thresholds may become site-family specific over time
- never silently lower thresholds to improve throughput

Hard stops:

- detected CAPTCHA
- required field unresolved
- contradictory candidate data
- unsupported upload flow
- suspicious page transition
- any Tier 3 extension present without explicit user approval

## 15. Persistence Model

Core tables:

- `jobs`
- `job_sources`
- `companies`
- `candidate_profiles`
- `resume_variants`
- `generated_documents`
- `applications`
- `application_attempts`
- `application_events`
- `answers`
- `field_mappings`
- `artifacts`
- `model_calls`
- `review_queue`

Minimum schema details:

- `jobs`
  - primary key
  - canonical URL unique index
  - external job id index
  - company id foreign key
  - ATS vendor index
  - discovered_at index
  - status index
- `job_sources`
  - job id foreign key
  - source type
  - source URL
  - source-specific external id
  - first_seen_at
  - last_seen_at
  - unique constraint on source type plus source URL or source external id
- `applications`
  - job id foreign key
  - candidate profile id foreign key
  - current state
  - last_attempt_id
  - applied_at
  - unique constraint on job id plus candidate profile id
- `application_attempts`
  - application id foreign key
  - mode
  - browser profile id or worker id
  - started_at
  - ended_at
  - result
  - failure_code
  - submit_confidence
- `field_mappings`
  - attempt id foreign key
  - field key
  - raw label
  - raw DOM signature
  - inferred type
  - confidence
  - answer id foreign key nullable
  - truth tier of answer (nullable, one of: `observed`, `inference`, `extension`)
- `answers`
  - canonical question hash
  - normalized question text
  - answer text
  - source type
  - approval status
  - truth tier (`observed`, `inference`, `extension`)
  - extension_approved (boolean)
  - interview_prep_notes (nullable, populated for Tier 3 extensions)
  - provenance_facts (list of Tier 1 fact IDs that support this answer)
  - last_used_at
- `artifacts`
  - attempt id foreign key
  - artifact type
  - path
  - size_bytes
  - checksum
  - created_at
- `model_calls`
  - stage
  - model provider
  - model name
  - prompt version
  - input size
  - output size
  - latency_ms
  - estimated cost
  - linked entity id

Indexing requirements:

- jobs by canonical URL, ATS vendor, status, discovered_at
- applications by current state and applied_at
- attempts by result and started_at
- answers by canonical question hash
- artifacts by attempt id and artifact type

## 16. Artifact Strategy
Store artifacts locally with stable references from the database:

- page screenshots
- Playwright traces
- HTML snapshots where useful
- generated resume files
- generated cover letter files
- model prompts and outputs
- answer packs

This is mandatory for debugging and replay.

Retention policy:

- keep structured records indefinitely unless user deletes them
- keep screenshots and generated documents for at least 180 days by default
- keep full Playwright traces and raw HTML snapshots for 30 to 60 days by default
- compress large trace artifacts where practical
- allow per-artifact retention overrides for bookmarked or failed attempts
- provide pruning commands and dashboard controls with projected disk savings before deletion

Storage policy:

- store artifacts under stable content-addressed or attempt-addressed paths
- compute checksum and byte size on write
- never delete artifacts referenced by an active review item or unresolved failure analysis

## 17. UI Requirements
The local dashboard should support:

- inbox of discovered jobs
- scoring view with reasons
- per-job detail page
- generated resume preview
- answer review page
- review queue
- application history
- failure analysis
- search and filters

Useful actions:

- approve for apply
- re-score
- re-tailor
- retry failed application
- mark as ignored
- mark as applied externally

## 18. CLI Requirements
Provide a clear CLI because many implementation and recovery tasks are operational:

- `discover`
- `enrich`
- `score`
- `tailor`
- `prepare-apply`
- `apply`
- `retry`
- `serve`
- `doctor`
- `replay-attempt`
- `export`
- `prune-artifacts`
- `session-status`
- `reauth`

## 19. Folder and Module Structure

Recommended Python package layout:

```text
jobbot/
  api/
  cli/
  config/
  db/
  discovery/
    linkedin/
    greenhouse/
    lever/
    workday/
    custom_sites/
  enrichment/
  scoring/
  tailoring/
  execution/
    browser/
    handlers/
    forms/
  review/
  tracking/
  artifacts/
  prompts/
  models/
  utils/
```

## 20. Implementation Phases

### Phase 0: Foundation

- set up repo, packaging, config, migrations, logging
- establish canonical schemas
- create candidate profile ingestion flow
- define prompt registry and prompt versioning scheme
- define confidence scoring interfaces and initial thresholds
- define artifact retention defaults

Phase 0 deliverables must include:

- initial schema DDL and migrations
- prompt templates stored with explicit version ids
- sample fixture corpus for Greenhouse, Lever, Workday, and LinkedIn
- local profile/session health checker

### Phase 1: Discovery MVP

- LinkedIn listing discovery
- Greenhouse/Lever/Workday listing discovery
- job normalization and deduplication
- dashboard inbox

Exit criteria:

- system can discover and store jobs reliably

### Phase 2: Enrichment and Scoring

- structured extraction cascade
- candidate-job matching
- score explanations

Exit criteria:

- user can sort jobs by useful score with clear reasons

### Phase 3: Tailoring

- resume variant generation with truth-tier tagging
- answer pack generation with tier labels
- optional cover letters
- strict fact-grounding with three-tier classification
- interview prep note generation for Tier 3 extensions
- review queue integration for Tier 3 approval workflow

Exit criteria:

- generated materials are usable and auditable
- every generated claim is tagged with its truth tier and source fact provenance
- Tier 3 extensions route to review queue and are blocked from auto-submit until approved

### Phase 4: Controlled Application Execution

- deterministic ATS handlers
- upload and question support
- review-before-submit flow
- attempt tracking and replay artifacts

Exit criteria:

- guarded apply works for Greenhouse and Lever on stable flows

### Phase 5: LinkedIn Easy Apply Assistance

- LinkedIn session handling
- question extraction
- draft/assist mode
- guarded submit on high-confidence flows only

Exit criteria:

- LinkedIn applications can be prepared and assisted safely

### Phase 6: Workday and Custom Site Expansion

- Workday family handlers
- custom careers detection
- low-confidence routing to assist mode

Exit criteria:

- coverage improves without unsafe submission behavior

## 21. Quality Requirements

### 21.1 Testing

- unit tests for normalization, scoring, and field mapping
- fixture-based extraction tests with saved HTML
- browser integration tests against mock forms
- regression tests for known ATS layouts

Selector robustness requirements:

- each ATS handler must include fixture-based tests against multiple historical page variants
- handler failures must classify as:
  - page changed but still recognizable
  - unsupported variant
  - authentication/session issue
  - browser/runtime issue
- when deterministic selector sets fail, capture a diagnostic snapshot and route to assist mode rather than guessing
- support a DOM signature mechanism per handler so meaningful markup drift can be detected and surfaced early

### 21.2 Reliability

- retries with backoff for network/model failures
- resumable pipeline state
- worker crash recovery
- idempotent application attempt records

### 21.3 Performance

- cache extracted jobs and model outputs where safe
- parallelize discovery and enrichment
- keep browser concurrency conservative

### 21.4 Cost Control

- estimate and record model cost per job and per application attempt
- maintain stage-level token and cost budgets
- prefer deterministic extraction and cached answer reuse before invoking LLMs
- expose daily and weekly model spend in the dashboard
- support hard budget ceilings that disable non-essential LLM calls when exceeded

Planning targets for initial implementation:

- average discovered job should cost near zero unless enrichment fallback is required
- average scored job should use one lightweight model pass at most
- average application should reuse prior answers whenever possible

### 21.5 Prompt Versioning

- every prompt template must have a stable version id
- every model call record must reference the prompt version used
- prompt changes must be reviewable in source control
- support replay of a prior extraction or scoring task with the old or new prompt version
- maintain a small golden dataset for evaluating prompt changes before rollout

### 21.6 Deduplication Specifics

Dedup strategy should be layered:

1. exact match on canonical URL
2. exact match on ATS external job id
3. strong match on normalized company plus normalized title plus normalized location
4. fuzzy match candidate generation using token-based similarity on title and company, then confirmation with description overlap or source metadata

Recommended initial algorithms:

- normalized token set similarity for titles
- domain-aware normalization for company names
- deterministic location normalization
- optional embeddings only later, and only for tie-breaks or difficult cases

The system must preserve provenance even when multiple source records map to one canonical job.

## 22. Non-Goals and Warnings

- This system should not aim to bypass every anti-bot measure.
- It should not pursue reckless mass automation.
- It should not hide the fact that some sites require human review.
- It should not optimize for vanity metrics like jobs submitted per hour.

The correct optimization target is high-quality, relevant, defensible applications with strong operator control.

## 23. What To Reuse from Prior Ideas
The following ideas from the reviewed "Sovereign Hunter" draft are worth keeping:

- staged pipeline design
- enrichment cascade of structured extraction before LLM fallback
- browser profile isolation
- provider abstraction for LLMs
- local dashboard instead of a heavy SPA by default
- native browser driver rather than reliance on external agent CLIs

The following should be adjusted or rejected:

- do not lead with "fully autonomous"
- do not make CDP itself the centerpiece; session realism and handler quality matter more
- do not include broad board coverage before ATS/company depth is solid
- do not rely on AI-driven navigation as the primary execution strategy

## 24. Implementation Instructions for Another AI Instance

1. Start by implementing the schema, config system, and persistence layer.
2. Build discovery and normalization before any application submission logic.
3. Implement Greenhouse and Lever execution first because they are usually easier to stabilize than Workday and LinkedIn.
4. Use deterministic handlers for known flows before adding any LLM browser reasoning.
5. Add artifact storage and replay support before aggressive automation.
6. Treat final submit as a separately gated action with explicit confidence checks.
7. Implement the three-tier truth model (observed facts, supported inferences, plausible extensions) from the start. Every generated claim must be tagged with its tier and linked to source facts.
8. Keep all stage outputs versioned so future prompt or parser changes do not silently corrupt prior records.
9. Build the review queue early — Tier 3 extensions depend on it, and it is also useful for Tier 2 first-use approval.
10. Generate interview prep notes for every Tier 3 extension at the time of proposal, not at the time of approval.
