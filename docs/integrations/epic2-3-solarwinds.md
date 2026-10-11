# Epic 2 & 3 — SolarWinds Service Desk Integration

**Scope:** the full technical spec, the IT/ITSM request template, and the live-tenant-verified corrections to the SolarWinds Service Desk (Samanage) API for the bidirectional ticketing integration (§8 of the delivery plan). **Status:** Epic 2 (config surface) complete 2026-09-18; Epic 3 (outbound push + inbound polling sync) complete 2026-09-19, including a review-fix pass, a volume-control pass, and a critical wire-format fix — see [`task-list.md`](task-list.md) for the full pass-by-pass completion record. See [`INTEGRATIONS_ROADMAP.md`](../../INTEGRATIONS_ROADMAP.md) for the big picture and the Documentation Map.

**Convention:** risk IDs (`R#`) referenced throughout this file are defined in [`risk-register.md`](risk-register.md) — there are many (R6-R9, R19-R33), and rather than annotate every single occurrence, treat any bare `R#` below as a pointer to that file. "§5" refers to [`epic1-sso.md`](epic1-sso.md); "§3"/"§10"/"§11" refer to the master `INTEGRATIONS_ROADMAP.md`'s Finalized Decisions (where assumptions A1-A3 live), Rollout Plan, and — historically, before this split — the Engineering Task List (now [`task-list.md`](task-list.md)).

---

## 8. Epic 2 & 3 — SolarWinds Service Desk: Technical Spec

### 8.0 IT/ITSM Team Request Template (send to whoever administers the target org's Service Desk instance)

Reusable per-deployment, same spirit as §5's Entra template (in `epic1-sso.md`) — fill in real values when standing this up against a given organization's SolarWinds Service Desk tenant. Each item below maps directly to a config value or model field in §8.2-§8.6, not a generic ITSM-integration checklist; these are per-tenant lookups the API spec itself can't supply.

**Account & access:**
- A **dedicated service-account admin user**, not a shared personal account — regenerating a token invalidates every prior token issued to that user (§8.3), so a shared personal account breaks unpredictably whenever anyone else on the team rotates their own token.
- The tenant's **subdomain** (e.g. `acme` in `acme.samanage.com`) — this is the real API base URL (§8.1), not the shared regional host below.
- Which **region** the tenant is on: `US` / `EU` / `APJ` — kept for support-contact purposes; the shared regional hosts (`api.samanage.com` etc.) do not serve ticket resources (§8.1), so this alone won't produce a working API base.
- **Plan tier**: Advanced (1000 calls/min) or Premier (1500 calls/min) — sets how aggressively the polling loop (§8.6) can run.
- A **sandbox/staging tenant, if one exists** — to test before creating real incidents in the live queue. If none exists, plan to test against a small, clearly-tagged subset in production instead.

**Tenant configuration values (feed directly into `SolarWinds_Instance`, §8.2) — these are free-text LABELS, not IDs (confirmed §8.1, no lookup endpoint exists for either):**
- **Workflow state labels**: the actual ticket states configured (e.g. one trial tenant had `New`/`Assigned`/`Awaiting Input`/`On Hold`/`Resolved`/`Closed` — don't assume a standard ITIL set), which one is "New/Open" (`open_state`), and which count as resolved/closed (`resolved_states`) — this is what tells the inbound poll (§8.6) when to mark a finding mitigated. Get the exact spelling right — sent as literal strings, and an invalid `state` value is at least rejected with a 422.
- **Priority label mapping**: the real Priority labels configured (e.g. one trial tenant had exactly `Low`/`Medium`/`High`/`Critical`, not `Urgent`), so DefectDojo's Critical/High/Medium/Low/Info severities map to real priority labels (`severity_priority_map`). **Get this exactly right** — an invalid priority label is *not* rejected, the API silently returns 200 and leaves the previous value unchanged (§8.1/R9), so a typo here fails silently in production with no error to catch it.
- **Site and Department**: which one DefectDojo-created incidents should default to — ask whether a Security/DevSecOps/AppSec department already exists or should be created.
- **Assignment group**: a support group to auto-assign to, or land unassigned for manual triage initially. Independent of the per-incident `assignee`, which DefectDojo fills from the mapped product's **technical contact** (§8.5) — both can be set on one incident, so ask for the group even if every product has a technical contact.

**Process, not just config:**
- **Expected volume tolerance** — roughly how many findings/week would become incidents, and whether their triage team is prepared for that. Surface this before the first sync run, not after their queue fills unexpectedly. **Turn the answer into `auto_push_severities` (§8.5), not a promise.** That field is the only thing standing between a 1,500-finding SCA import and 1,500 incidents; `["Critical", "High"]` is the recommended starting posture, and everything else stays reachable one finding at a time from the Add/Edit Finding checkbox. It ships empty, so asking this question is a prerequisite to the integration doing anything automatically at all.
- **An escalation contact** for API issues (auth failures, rate-limit hits) — who gets paged if the service-account token expires or the integration starts erroring.

**Confirmed live, not just inferred (2026-09-18, against a personal trial tenant):** R8 (pagination), the real API base URL (tenant subdomain, not the shared regional host), and that `state`/`priority` write as plain labels rather than the numeric IDs the spec's schema implies — see §8.1 for the full writeup. `category` does match the spec (nested `{"name": ...}` object, §8.5) — don't assume every field follows the bare-label pattern. Re-verify the actual label sets (state/priority) per target tenant; they're confirmed to vary and can't be assumed to match the trial tenant's values.

**Severity mapping decision (2026-09-18):** `severity_priority_map` covers Critical/High/Medium/Low only — Info findings intentionally don't create tickets at all (skip-on-unmapped-severity rule, §8.5), a deliberate choice to keep the ticket queue to things worth a human's time rather than defaulting Info to the lowest priority.

### 8.1 Source of truth

The real, current OpenAPI spec for SolarWinds Service Desk (Samanage) — `resolved_schema.json` in the repo root — title "SolarWinds Service Desk API" v0.2.20, servers `api.samanage.com` (US) / `apieu.samanage.com` (EU) / `apiau.samanage.com` (APJ). It's ~1.4MB; read specific `paths.*` entries rather than the whole file.

**Confirmed: zero webhook support.** A webhook-looking doc exists at `support.incidents.cloud.solarwinds.com` but documents a *different* SolarWinds product (Incident Response/Observability, built on the acquired Squadcast platform — on-call alerting, unrelated to Service Desk ITSM). Grepping the real spec found zero webhook mentions. Inbound sync is Celery-beat polling, full stop — do not scope a webhook path.

**Confirmed against a live trial tenant (2026-09-18) — the spec's `servers` block is wrong for ticket resources.** `api.samanage.com` (the documented shared host) returns a bare `404 Not Found` (plain text, not a JSON error) for `/incidents`, `/problems`, `/changes`, and `/risks` — on every account, not just an empty/unconfigured one (reproduced with a real incident already created). Meanwhile `/sites`, `/departments`, `/categories`, `/groups`, `/roles`, `/users` all resolve fine on that same shared host. **The actual working base URL is the tenant's own subdomain** (e.g. `https://<tenant>.samanage.com/` — the same host the web UI lives on), which serves every resource including incidents. `SolarWinds_Instance` needs a `subdomain`/`base_url` config field per tenant; `region` alone (US/EU/AU) is not enough to construct a working API base URL. Client code should default to the tenant subdomain for all calls rather than assuming the shared regional host works.

**Confirmed: `state_id`/`priority` in the spec's request-body schema are wrong — write with plain labels, and the field is named `state`, not `state_id`.** Empirically tested via a live `PUT /incidents/{id}`:
- `{"incident": {"state": "New"}}` — accepted (200), field name is **`state`**, not `state_id` as documented. Sending numeric IDs (tried 1-15) or `state_id` as the key both fail with `422 {"state": ["Invalid state for account."]}`.
- `{"incident": {"priority": "Medium"}}` — accepted (200), also a plain label, matching what the spec loosely allowed (`anyOf: string`).
- Confirmed valid `state` labels for this tenant: `New`, `Assigned`, `Awaiting Input`, `On Hold`, `Resolved`, `Closed`. (`In Progress`, `Acknowledged`, `Pending` were all rejected — don't assume standard ITIL state names carry over.)
- Confirmed valid `priority` labels: `Low`, `Medium`, `High`, `Critical`. (`Urgent` is *not* valid for this tenant.)
- **Dangerous asymmetry:** an invalid `state` value correctly 422s, but an invalid `priority` value silently returns `200` and **keeps the previous value unchanged** — no error, no indication anything was rejected. The integration must validate `priority` against a known-good allowlist client-side before every write; the API will not catch a typo or a stale mapping.

**Net effect on §8.2's model:** `open_state_id`/`resolved_state_ids`/`severity_priority_map` should store **label strings**, not opaque numeric IDs — there is no `/states` or `/priorities` lookup endpoint (confirmed 404 on both), so IDs would be undiscoverable anyway. Labels are tenant-configured free text (confirmed to vary — this tenant doesn't have "In Progress" or "Urgent"), so they still must come from live discovery per tenant (read an existing incident, or literally attempt a `PUT` and see what the API accepts), never hardcoded across deployments.

**R8 (pagination) — confirmed, not just inferred.** `GET /incidents.json?per_page=1` against the live tenant returns standard Samanage pagination: `Link` header (`rel="first"/"next"/"last"`) plus `X-Total-Count`, `X-Total-Pages`, `X-Current-Page`, `X-Per-Page` headers. `?page=&per_page=` query params work as assumed in §8.6.

**Wire-format requirement confirmed 2026-09-19, and load-bearing — see R33.** Every request path needs a mandatory `.json` extension; `Accept: application/vnd.samanage.v2.1+json` alone does not get a real tenant to negotiate JSON — a bare path 406s regardless of verb or depth (`GET /sites`, `GET/PUT /incidents/{id}`, `POST /incidents/{id}/comments` all confirmed). This is centralized in `client.py`'s `_json_path()` (§8.3) — see R33 for the full incident, including why four build passes' mocked test suites never caught it.

### 8.2 Module — `dojo/solarwinds/` (canonical `dojo/url/` layout per `AGENTS.md`)

A bespoke module, not an extension of `Tool_Configuration` — this is a JIRA-shaped domain (instance config + per-product routing + finding↔ticket link table), which the generic tool-config credential bag doesn't model. Reuses the **encryption** pattern `tool_config` already has, explicitly avoiding the JIRA module's plaintext `password = CharField` anti-pattern (`dojo/jira/models.py:13`).

```python
class SolarWinds_Instance(models.Model):
    configuration_name = models.CharField(max_length=255, unique=True)
    subdomain = models.CharField(max_length=255)  # e.g. "acme" -> https://acme.samanage.com/ ; the actual working API base (see §8.1) — region alone doesn't get you a working host
    region = models.CharField(max_length=3, choices=[("US","US"),("EU","EU"),("AU","APJ")], default="US")  # kept for reference/support-contact purposes; not used to build the API URL
    api_token = models.CharField(max_length=900)   # encrypted via dojo_crypto_encrypt
    service_account_email = models.CharField(max_length=255, blank=True)
    default_site_id = models.CharField(max_length=64, blank=True)
    default_department_id = models.CharField(max_length=64, blank=True)
    default_category = models.CharField(max_length=255, blank=True)  # LABEL, not an id — e.g. "Security" (§8.5); written as a nested {"name": ...} object on the wire, confirmed different shape than state/priority
    open_state = models.CharField(max_length=64)              # tenant-specific LABEL, not an ID (§8.1 — no numeric state_id exists)
    resolved_states = models.JSONField(default=list)          # tenant-specific LABELs, e.g. ["Resolved", "Closed"]
    severity_priority_map = models.JSONField(default=dict)    # {"Critical": "<tenant priority label>", ...} — labels, not IDs; validate against a live-fetched allowlist before every write (API silently no-ops on an invalid priority label, see §8.1). This is "what can EVER be pushed", not "what pushes automatically"
    auto_push_severities = models.JSONField(default=list)     # added 2026-09-19 — the subset of the map's keys that a push_all_findings product files automatically, e.g. ["Critical","High"]. Default EMPTY, not "everything mapped" (R32). Mapped-but-not-listed severities stay pushable one finding at a time from the Add/Edit Finding checkbox
    push_enabled = models.BooleanField(default=False)
    poll_enabled = models.BooleanField(default=False)

class SolarWinds_Product(models.Model):   # per-product routing, analogous to JIRA_Project
    solarwinds_instance = models.ForeignKey("dojo.SolarWinds_Instance", on_delete=models.CASCADE)  # string ref per AGENTS.md, including among classes moved together
    product = models.ForeignKey("dojo.Product", on_delete=models.CASCADE)
    site_id = models.CharField(max_length=64, blank=True)
    department_id = models.CharField(max_length=64, blank=True)
    group_assignee_id = models.CharField(max_length=64, blank=True)
    push_all_findings = models.BooleanField(default=False)

class SolarWinds_Ticket(models.Model):    # link table, analogous to JIRA_Issue (dojo/jira/models.py:177)
    finding = models.OneToOneField("dojo.Finding", on_delete=models.CASCADE)
    incident_id = models.CharField(max_length=64, db_index=True)
    incident_number = models.CharField(max_length=64, blank=True)
    last_synced_state = models.CharField(max_length=64, blank=True)  # reconciliation cursor — a LABEL (see §8.1), not an ID
    external_updated_at = models.DateTimeField(null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True)
```

**FK convention, corrected 2026-09-19.** Both `solarwinds_instance` foreign keys shipped as direct class references (`models.ForeignKey(SolarWinds_Instance, …)`) while the `product`/`finding` keys in the same file correctly used string refs. `AGENTS.md` mandates string refs for references *among* classes defined together in the same `models.py`, not only for outward ones. Both are now `"dojo.SolarWinds_Instance"`; `makemigrations --check` still reports "No changes detected", as AGENTS.md guarantees, since Django resolves either form through the app registry.

**Why `state`/`priority` are tenant config, not enums:** confirmed live (§8.1) — both are free-text labels defined per tenant (this test tenant's valid states are `New`/`Assigned`/`Awaiting Input`/`On Hold`/`Resolved`/`Closed`; valid priorities are `Low`/`Medium`/`High`/`Critical` — neither matches a fixed, guessable set, and there's no lookup endpoint for either). Real values must be resolved from the live tenant at config time (read an existing incident, or test a `PUT`) and stored in `open_state`/`resolved_states`/`severity_priority_map` — never hardcoded across deployments.

### 8.3 API client — `dojo/solarwinds/client.py`

**Base URL is per-tenant, not the shared regional host** (§8.1): `base_url = f"https://{instance.subdomain}.samanage.com"`. The documented shared host (`api.samanage.com` etc.) 404s on `/incidents` and the other ticket-object resources even though it works for config resources — don't use it for anything ticket-related.

Thin `requests` wrapper. Every call sends:
```
X-Samanage-Authorization: Bearer <decrypted api_token>
Accept: application/vnd.samanage.v2.1+json
Content-Type: application/json
```
Methods: `create_incident(payload)` → `POST /incidents`; `get_incident(id)` → `GET /incidents/{id}`; `list_incidents_updated_since(...)` → `GET /incidents` using the spec's `Updated_at` filter (incremental polling, not full scans); `add_comment(incident_id, body, is_private=True)` → `POST /incidents/{incident_id}/comments`. Centralize 429 handling with backoff here.

**Mandatory `.json` path extension — added 2026-09-19, see R33.** Every method routes through `_url()`, which now calls a central `_json_path()` to append `.json` to the path (idempotent, splits off any query string first so pagination composes as `/incidents.json?page=2&per_page=100`). Confirmed live: `Accept` header alone does not work, a bare path 406s on every verb (GET/PUT/POST) at every depth (collection, member, sub-resource). This was broken for the client's entire history until an operator hit it via Test Connection — see R33 for the full root-cause writeup and what it means for testing any future HTTP client code in this module.

**Retry scope — three classes, corrected 2026-09-19 (R29).** Retries cover (a) `429`/`502`/`503`/`504` responses and (b) `requests.RequestException` — transport failures that never produced a response at all (connection reset, read timeout, DNS). Class (b) originally escaped the retry loop on the first attempt, so an ordinary network blip permanently failed a push while a 503 from the same tenant got four attempts. Every other 4xx stays non-retryable on purpose: deterministic, and retrying burns rate-limit budget (R7) for the same answer.

**Validate `priority` client-side before every write.** Confirmed live: an invalid `state` value correctly 422s, but an invalid `priority` value silently returns `200` and leaves the incident's priority unchanged — no error surfaces. `create_incident`/`update_incident` must check the outgoing priority label against `instance.severity_priority_map`'s known-good values before sending, since the API gives no feedback on a bad one.

**Auth details (from the spec):** token generated per-admin-user in the Service Desk UI; **regenerating invalidates all prior tokens for that user.** Use a **dedicated service-account admin** for the integration (A3, in the master `INTEGRATIONS_ROADMAP.md`'s Finalized Decisions §3), not a shared personal token — plus a scheduled health-check task pinging the API and alerting on 401.

**Rate limits:** 1000 calls/min (Advanced plan) / 1500/min (Premier) — the polling design must use one batched, filtered query per instance, never per-ticket GETs.

### 8.4 Credential encryption

`api_token` stored via `dojo_crypto_encrypt()` on save / `prepare_for_view()` on read — `dojo/utils.py:1026`, AES-256-GCM, the exact pattern `dojo/tool_config/ui/views.py:57,80` already uses. Mirror JIRA's "keep existing value if field left blank on edit" form behavior (`AdvancedJIRAForm.__init__`/`clean`, **`dojo/jira/forms.py:79-87`** — corrected 2026-09-18 from the "86-93" cited here previously). **Built 2026-09-18** in `dojo/solarwinds/ui/forms.py`, with one deliberate strengthening over JIRA: the field is excluded from `Meta.fields` rather than merely re-widgeted, so no code path can write a rendered value back to the column. See R21.

### 8.5 Outbound flow (DefectDojo → SolarWinds)

`dojo/solarwinds/services.py` builds the incident payload from a Finding:

| SolarWinds field | Source |
|---|---|
| `incident.name` (≤255 chars) | `f"[DefectDojo] {finding.title}"[:255]` |
| `incident.description` | Finding description + severity + deep link back to the finding |
| `incident.state` | `instance.open_state` (label, e.g. `"New"` — field name is `state`, not `state_id`, see §8.1) |
| `incident.priority` | `instance.severity_priority_map[finding.severity]` (label, validated client-side, see §8.3) |
| `incident.category` | `{"name": instance.default_category}` — e.g. `{"name": "Security"}` (created via `POST /categories`, confirmed live against a personal trial tenant). **Unlike `state`/`priority`, this one matches the spec**: confirmed live it's a nested object with a `name` key, not a bare string — don't assume every field follows the same bare-label pattern just because two of them did. |
| `incident.site_id` / `department_id` | `SolarWinds_Product` override → instance default |
| `incident.group_assignee_id` | `SolarWinds_Product.group_assignee_id` |
| `incident.assignee` | `{"email": product.technical_contact.email}` — added 2026-09-19. Nested object keyed on `email`, **verified against the spec** (`anyOf` [object requiring `email`, the nil enum]), same family as `category` and not the bare-label shape `state`/`priority` use. Omitted entirely when the product has no technical contact or the contact has no email — the schema does accept an explicit nil, but sending one would *clear* an assignee rather than leave the tenant's own default alone. **Independent of `group_assignee_id`**: a person and a support group are separate fields on a Samanage incident, not alternatives, and both may be present on one incident |
| `incident.tag_list` | `"defectdojo,finding-{id}"` |

**Severity → priority mapping decision (2026-09-18, confirmed against the trial tenant's 4 priority labels):** `severity_priority_map` covers only `Critical`→`Critical`, `High`→`High`, `Medium`→`Medium`, `Low`→`Low` — **no `Info` key**. `_save_finding_with_solarwinds_sync` (§8.5's hook point) must **skip the push entirely when `finding.severity` has no entry in `severity_priority_map`**, rather than defaulting Info to the lowest priority — the decision was to keep Info findings DefectDojo-only rather than adding ticket-queue volume the triage team didn't ask for. This also means any severity a future tenant's map doesn't cover is skipped by the same rule, not just Info specifically — no separate feature flag needed.

**Auto vs. manual — the severity split, added 2026-09-19.** `severity_priority_map` was doing two jobs: the priority-label translation *and* the only eligibility gate, so `push_all_findings=True` pushed every finding of every mapped severity with no volume control at all. One SCA/SAST import of ~1,500 findings across Critical→Low is ~1,500 individual `POST /incidents` calls — there is no batching, no summary incident and no cap anywhere in this API, and `SolarWinds_Ticket.finding` is a `OneToOne`, so it is strictly one finding to one incident. The two questions are now two fields:

| | `severity_priority_map` | `auto_push_severities` |
|---|---|---|
| Question | Can this severity **ever** become a ticket, and under what priority label? | Does it fire **with nobody asking**, on a `push_all_findings` product? |
| Shape | `{"Critical": "Critical", …}` | `["Critical", "High"]` — a subset of the map's **keys** |
| Default | unchanged | **`[]`** — nothing automatic (R32) |
| Enforced by | `services.build_incident_payload` raises `UnmappedSeverityError`; `clean()` rejects unknown/blank keys | `helper.resolve_push_target` on the `push_requested=False` branch only; `clean()` rejects a non-subset |

Read the branch in `resolve_push_target` as the whole of the feature: `push_requested=True` (a human ticked the box) takes any mapping and needs only a priority label; `push_requested=False` additionally requires `mapping.push_all_findings` **and** `instance.is_auto_push_severity(finding.severity)`. So a Medium finding on a `push_all_findings` product with `auto_push_severities=["Critical","High"]` does **not** auto-file, and the same finding files fine when somebody asks for it.

**`is_push_all_findings()` is deliberately left severity-blind** and must stay that way. It is the *per-batch* gate in `post_process_findings_batch`, evaluated once against `findings[0]`; making it severity-aware would mean a single Medium finding sorted to the head of an import silently suppressed every Critical behind it. Severity is filtered per finding, one layer down. Two companion predicates were added for the UI instead: `is_push_all_findings_auto(finding)` (the per-finding form of the same question — drives the checked-and-disabled checkbox) and `is_manual_push_available(finding)` (mapping exists **and** severity is mapped — decides whether the checkbox is rendered at all).

**Why the default is empty and not "every key in the map".** Every other switch in this module defaults to off (`push_enabled`, `poll_enabled`, `push_all_findings`), and the failure mode of a permissive default here is not a stray ticket, it is a flood into someone else's queue. Migration `0281` is therefore a bare `AddField` with **no backfill** — see R32 for the cost of that choice and who needs telling.

After create: write the `SolarWinds_Ticket` link row, then post a correlation comment (`POST /incidents/{id}/comments`, `is_private=True`) for audit trail.

**Hook point:** mirror JIRA's explicit-parameter pattern (`finding.save(push_to_jira=...)` threaded through `dojo/finding/helper.py:1238` — citation **re-verified accurate** 2026-09-18), not a signal — add a parallel `_save_finding_with_solarwinds_sync`, dispatched async via Celery.

**Built 2026-09-18, with one departure from the above.** JIRA's `push_to_jira` is computed *at each call site* as `is_push_all_issues(finding) or <the form checkbox>`; nothing inside `Finding.save()` consults `push_all_issues`. SolarWinds keeps that plumbing (`Finding.save(push_to_solarwinds=…)` → `post_process_finding_save` → `helper.push_finding()` → Celery) but resolves the `push_all_findings` half **inside the post-save hook**, so the config-driven mode works everywhere a finding is saved without editing ~15 call sites and both template trees for a checkbox. See [`task-list.md`](task-list.md)'s Epic 3 entry and R26. Orchestration is `dojo/solarwinds/helper.py`; the Celery tasks are `dojo/solarwinds/tasks.py`. `services.py` stays pure and `client.py` stays HTTP-only.

**"Everywhere a finding is saved" was an overstatement until 2026-09-19 — see R28.** The hook rides inside `post_process_finding_save_internal`, which only runs when `Finding.save()`'s post-processing gate opens, and that gate is a disjunction of the post-processing *options*. Callers passing all of them False (`save_no_options()`, and `jira.helper.finding_link_jira` / the SonarQube status updater when product grading is off) skipped it entirely and silently. The gate now has an `else` branch that runs the SolarWinds half alone, with the importer call sites opting out via `defer_post_processing=True` because `post_process_findings_batch` already covers them once per batch.

**The manual per-finding trigger, added 2026-09-19 — R26's UI half.** `push_to_solarwinds` had been threaded all the way through `Finding.save()` since the first pass with nothing anywhere setting it to `True`, so an operator's only control was per-product all-mapped-severities-or-nothing. There is now a checkbox, in **both** template trees, on the two screens where a human is actually looking at one finding and deciding:

| Surface | Where | Wiring |
|---|---|---|
| Add Finding | `dojo/test/ui/views.py` `AddFindingView` | `get_solarwinds_form()` → `context["sform"]` → `process_solarwinds_form()` → `finding.save(push_to_jira=…, push_to_solarwinds=…)` |
| Edit Finding | `dojo/finding/ui/views.py` `EditFinding` | same three methods, same final `new_finding.save(...)` on both the duplicate-clearing and ordinary branches |
| REST API | `FindingSerializer` / `FindingCreateSerializer` | `push_to_solarwinds` write-only boolean → `helper.push_finding_explicitly()` after the save |
| Templates | `add_findings.html`, `edit_finding.html` in `dojo/templates/` **and** `dojo/templates_classic/` | `{% if sform %}` block, mirroring `{% if jform %}` |

The form is `SolarWindsFindingForm` in `dojo/solarwinds/ui/forms.py`, built by `build_finding_form()`. It is one field, not JIRA's fifteen, because JIRA's equivalent also owns issue linking/unlinking and finding-group routing — none of which exists here, since there is **no outbound "update an existing incident" path at all**. Three behaviours are worth not losing:

- **The section is not rendered when it would do nothing.** `build_finding_form()` returns `None` — and the template then renders nothing — unless `any_push_mapping_exists()`, this product has a push-enabled mapping, and (on edit, where the severity is known) `is_manual_push_available()`. Same `{% if jform %}`-shaped gate JIRA uses, with a stricter question behind it.
- **Checked-and-disabled for the two states where the choice is not the operator's**: the severity auto-pushes anyway, or the finding already has an incident (named in the help text, since a second push is swallowed by `has_ticket`).
- **`clean()` validates against the severity in *this* submission**, read off `finding_form`, not the stored one — on add-finding there is no stored severity at all, and on edit it may be changing in the same POST. Directly modelled on `JIRAFindingForm.clean` reaching into `self.finding_form` for active/verified.

**Deliberate deviation from JIRA at the call sites, and the one most likely to get "fixed" back.** JIRA computes `push_to_jira = is_push_all_issues(finding) or <checkbox>` at every call site because nothing inside `Finding.save()` consults `push_all_issues`. The SolarWinds call sites pass **the checkbox alone**: `push_all_findings` is resolved *inside* the post-save hook here, so OR-ing it in would be a no-op that implies a mechanism that is not there. The auto half still reaches the form, as `push_all`, which is what renders the box checked and disabled — so the operator sees exactly what JIRA's operator sees.

**Scope held deliberately narrow, and stated rather than skipped silently.** Not wired: **bulk edit** (`FindingBulkUpdateForm`) and **add-finding-from-template** (`add_temp_finding`). Bulk edit is the single most dangerous place to put this control — one careless multi-select is exactly the flood `auto_push_severities` was added to prevent, and it wants a confirmation step and a count, which is its own change. `add_temp_finding` shares `add_findings.html` but is a separate function-based view; the `{% if sform %}` block sits only in the branches that are actually populated, so wiring it later is a view-only change. Also not built, because it does not exist to be wired: any "keep in sync"/update-existing-incident path, which is the bulk of JIRA's ~15 call sites.

**The status gate is load-bearing, not defensive.** `helper.is_status_pushable()` refuses to open an incident for a finding that is mitigated, false-positive, out of scope, a duplicate or risk accepted. Without it, *closing* a finding on a `push_all_findings` product would open a ticket for it, because the close path calls `Finding.save()` like every other write.

### 8.6 Inbound flow (SolarWinds → DefectDojo, Celery-beat)

Register in `CELERY_BEAT_SCHEDULE` (**`settings.dist.py:954`** — the `:906` cited here previously had drifted; corrected 2026-09-18, the `solarwinds-poll-resolved` entry as built sits at `:1018`). The task module also has to be named in `CELERY_IMPORTS`: `app.autodiscover_tasks()` only scans `<installed_app>/tasks.py` and `dojo.solarwinds` is a package inside the `dojo` app, so without it a beat entry naming `dojo.solarwinds.tasks.*` raises `NotRegistered` in the worker.

```python
"solarwinds-poll-resolved": {
    "task": "dojo.solarwinds.tasks.poll_resolved_incidents",
    "schedule": crontab(minute="*/10"),
    "options": {"expires": 9 * 60},
},
```
Logic: for each `poll_enabled` instance, call `list_incidents_updated_since(...)`; join against open `SolarWinds_Ticket` rows; where `state` ∈ `resolved_states`, mitigate the linked finding (`active=False`, `is_mitigated=True`, `mitigated=now()`); update the `last_synced_state` cursor for idempotency. This cursor design means a future webhook (should SolarWinds ever add one) could call the same reconciliation function with zero schema change — a latent capability, not a committed enhancement.

**Built 2026-09-18 as `helper.reconcile_resolved_incidents()`, with two corrections to the sketch above.** First, the join is scoped by `solarwinds_instance` *before* matching on `incident_id`, because incident ids are only unique within a tenant — see R24, which this pass found and closed with a schema addition. Second, the mitigation does not set those three fields directly: it calls **`finding_helper.close_finding`**, which additionally handles endpoint/location statuses, risk-acceptance removal, the external-issue close, the JIRA status sync, an audit note and the `finding_closed` notification. The finding's existing `false_p`/`out_of_scope`/`duplicate` values are passed back into it unchanged (it assigns all three unconditionally, so passing `False` would quietly clear a human's verdict), and findings already marked false-positive or out of scope are skipped outright while their cursor still advances.

**Reconciliation is bidirectional as of 2026-09-19 — see R30.** The sketch above (and the first implementation) only handled not-resolved → resolved. A technician reopening a resolved incident left the finding mitigated forever, and the *next* resolution was then a silent no-op because `_mitigate_from_incident` short-circuits on `finding.is_mitigated`. `_reconcile_one` now reads both directions from the cursor and calls a new canonical `finding_helper.reopen_finding()` on the way back, guarded so that **only closures this integration made** are undone: the guard is `finding.mitigated_by == get_solarwinds_actor()`, the same actor `_mitigate_from_incident` records. A human's closure is logged and skipped while the cursor still advances — the same shape as the existing `false_p`/`out_of_scope` rule, applied in the other direction.

**The 10-minute schedule does not mean a 10-minute query window — see R20.** The `Updated_at` filter only offers fixed day buckets or a day-granular date range, so the narrowest window this task can request is one day. `list_incidents_updated_since()` returns a superset of what was asked for; the reconciliation above must stay idempotent and must decide "did this actually change?" from the incident's own state versus `last_synced_state`, never from its mere presence in the response.

**R8 — confirmed (2026-09-18), no longer open.** Live tenant confirmed standard Samanage pagination: `GET /incidents.json?page=&per_page=` works as assumed, response carries a `Link` header (`rel="first"/"next"/"last"`) plus `X-Total-Count`/`X-Total-Pages`/`X-Current-Page`/`X-Per-Page` headers. Use `X-Total-Pages` (or follow `rel="next"`) to drive the poll loop's page cursor.
