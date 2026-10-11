# Epic 0 — Authorization Reactivation (RBAC)

**Scope:** the full technical spec for reactivating this fork's dormant pre-3.0 RBAC schema (§7 of the delivery plan) — role-based access control replacing the all-or-nothing `authorized_users` model. **Status: Active**, spec finalized 2026-08-09; PR 7 (flag defaulted to `shadow`) landed 2026-08-11 — see [`task-list.md`](task-list.md) for the full PR-by-PR completion record. See [`INTEGRATIONS_ROADMAP.md`](../../INTEGRATIONS_ROADMAP.md) for the big picture, why this epic exists (superseded reasoning in its own §2), and the Documentation Map.

**Convention:** risk IDs (`R#`) referenced below are defined in [`risk-register.md`](risk-register.md). "§2" refers to the master `INTEGRATIONS_ROADMAP.md`'s "Why This Doesn't Require Rebuilding DefectDojo Pro's RBAC" section; "§6" to [`epic1-sso.md`](epic1-sso.md); "§9" to [`risk-register.md`](risk-register.md); "§10" to the master roadmap's Rollout Plan.

---

## 7. Epic 0 — Authorization Reactivation (Active)

**Trigger:** manual per-user access grants via `authorized_users` turned out to be all-or-nothing (no read-only tier) — blocking "stakeholders get read-only per project, dev team gets edit access per project" without hand-managing every individual. **A2 is now permanently confirmed false** — this fork will never install the paid `pro` package — which removes the only reason Epic 0 was deferred (designing for Pro-coexistence). Epic 1.5 (SSO group-claims sync) stays fully deferred and is not required for any of this — role/group assignment remains 100% manual, same operating model as `authorized_users` today.

Drafted 2026-08-09 by three specialized planning passes (architecture, backend, frontend) against the live codebase and database, then reconciled where they conflicted. Two corrections from that reconciliation, verified directly against the running stack — cited because both the codebase and future readers should trust this section's numbers over any single pass's:
- Next migration number is **`0278`** (latest applied is `0277_seed_deduplication_execution_mode`).
- `dojo_product_type_member` has **1 real row today** (fixture-seeded: `product_type=1, user=1 (admin), role=4 (Owner)`, loaded by `dojo/fixtures/product_type.json` via `docker/entrypoint-first-boot.sh`) — not empty. This row goes live functionally the moment enforcement is enabled (§7.3). Harmless here since the admin account is already superuser, but every fresh install reproduces it, and it's a genuine semantic change worth calling out in the cutover PR rather than discovering later.

### 7.1 What's already there vs. what needs building

The 7 legacy RBAC tables (`dojo_dojo_group`, `dojo_dojo_group_member`, `dojo_role`, `dojo_product_member`, `dojo_product_group`, `dojo_product_type_member`, `dojo_product_type_group`, `dojo_global_role`) physically exist, schema-correct, in the live database — created by pre-3.0 migrations, never dropped, currently declared `managed=False` in `dojo/authorization/models.py` and consulted by nothing. `dojo_role` is seeded: `Reader`, `Writer`, `Maintainer`, `Owner` (`is_owner=True`), `API_Importer`. No UI, no REST API, and no enforcement logic reference any of the eight classes anywhere in the current tree — confirmed by reading `dojo/authorization/authorization.py`, `dojo/authorization/query_registrations.py`, and the "moved to Pro" comments in `dojo/urls.py` (~lines 104-133).

The full pre-3.0 implementation is recoverable from git history at commit `db1932c9e` (immediate parent of `952a56d13`, "Tailwind UI rebuild, legacy authorization, OS surface removals" — the actual OS-3.0 split commit) — but it is **reference material, not a patch source**. That commit predates a substantial rewrite: call sites keyed on the `Permissions` enum dropped from 910 to 10 between `db1932c9e` and today, replaced by an `Action`-string vocabulary (`"view"`/`"edit"`/`"delete"`/`"add"`/`"import"`) and two subsystems that didn't exist at the old commit — `dojo/authorization/url_permissions.py` (317-line URL→permission map) and `dojo/authorization/middleware.py` (permission checks moved off per-view decorators into middleware). **Porting the old `authorization.py` verbatim is not viable** — it would mean reverting ~900 call sites or running two incompatible permission vocabularies side by side, both far outside this epic's scope. Decision: **keep the current `Action`-string interface**; port role *semantics*, not the enum. Everything below reflects that decision.

### 7.2 Migration — `dojo/db_migrations/0278_reactivate_rbac_models.py`

Verified empirically against the live stack: because migration `0268` flipped these models with `AlterModelOptions` (state-only) rather than `DeleteModel`, they're still in Django's migration state with their original field definitions. Flipping `managed=False → True` back is therefore **zero DDL** — confirmed by running `makemigrations --check --dry-run` and `sqlmigrate` against the container.

```python
class Migration(migrations.Migration):
    dependencies = [("dojo", "0277_seed_deduplication_execution_mode")]
    operations = [
        migrations.AlterModelOptions(name="dojo_group", options={}),
        migrations.AlterModelOptions(name="dojo_group_member", options={}),
        migrations.AlterModelOptions(name="global_role", options={}),
        migrations.AlterModelOptions(name="product_group", options={}),
        migrations.AlterModelOptions(name="product_member", options={}),
        migrations.AlterModelOptions(name="product_type_group", options={}),
        migrations.AlterModelOptions(name="product_type_member", options={}),
        migrations.AlterModelOptions(name="role", options={"ordering": ("name",)}),
    ]
```

**The one real drift, and it's required for correctness, not just cleanliness:** every FK/M2M in the current shells carries `related_name="+"` (added specifically to avoid clashing with Pro's accessors — now pointless). Restore the `db1932c9e` related names in `dojo/authorization/models.py` alongside the migration: `Dojo_Group.users` → `related_name="users"`; every other `related_name="+"` on `Dojo_Group.auth_group`, `Dojo_Group_Member.{group,user,role}`, `Global_Role.{user,group,role}`, `Product_Member.{product,user,role}`, `Product_Group.{product,group,role}`, `Product_Type_Member.{product_type,user,role}`, `Product_Type_Group.{product_type,group,role}` → delete the argument entirely (historical models had none). This is load-bearing: `Global_Role.user`/`.group` with `related_name="+"` means `user.global_role` doesn't exist, and the reactivated engine reads it; the queryset filters in §7.4 need `product_member__user`-style reverse lookups that don't exist under `"+"` either. Rewrite the module/class docstrings — they currently instruct the reader that Pro owns these tables, which is now permanently false.

**Verification gate, mandatory in the PR:**
```bash
docker exec django-defectdojo_uwsgi_1 python manage.py sqlmigrate dojo 0278   # must emit ONLY BEGIN/COMMIT
docker exec django-defectdojo_uwsgi_1 python manage.py makemigrations --check --dry-run  # must report "No changes detected"
```

**Companion decisions, each deliberately scoped out of `0278` itself:**
- **Restore `Product.members` / `Product.authorization_groups` / `Product_Type.prod_type_members` / `.product_type_groups` accessors** (dropped state-only by `0268`). Recommend yes, via `SeparateDatabaseAndState` with empty `database_operations` in the same migration, so a reviewer sees the no-DDL intent explicitly.
- **`System_Settings.default_group{,_role,_email_pattern}`** — these were dropped with *real* DDL by `0268` (confirmed gone from the live table) and exist only to auto-assign SSO users to a default group, which is squarely Epic 1.5 territory. **Leave dropped.** Revisit only if Epic 1.5 is ever picked up.
- **Uniqueness** — no membership table has a DB constraint on `(scope, user)`/`(scope, group)`; duplicates were only prevented by old serializer `validate()` logic. Tables are effectively empty today (one fixture row), so adding `unique_together` now is cheap — but it's real DDL. Do it as a **separate follow-up migration `0279`** after `0278` lands clean, not bundled in. (Note: per [`task-list.md`](task-list.md), this item was renumbered to `0280` on 2026-09-18 since `0279` was taken by the SolarWinds integration's table-creation migration — this section is left as originally drafted, not retroactively edited, per the historical-record convention for this file.)

### 7.3 Authorization engine — fix the matrix, extend `Action`, remove two short-circuits

`dojo/authorization/roles_permissions.py` already has an `Action`-string role matrix (`get_roles_with_permissions()`) — it's just wrong in a way that defeats the entire point of this epic:

| Role | Current (live, broken) | Corrected |
|---|---|---|
| `Reader` | `{view, add}` | `{view}` — the current `add` grant would let "read-only" stakeholders create Engagements/Findings/etc. This is the change the whole epic exists to make. |
| `Writer` | `{view, add, edit, import, delete}` | `{view, add, edit, import}` — historically Writer could not delete Findings/Tests/Engagements. Flattening had granted full delete. |
| `Maintainer` | `{view, add, edit, import, delete, staff_only}` | `{view, add, edit, import, delete, manage}` |
| `Owner` | identical to Maintainer (so `Role.is_owner` currently carries no authority) | `{view, add, edit, import, delete, manage, own}` |
| `API_Importer` | `{view, add, edit, import}` | unchanged |

Extend `Action` to give Owner real teeth: add `Manage` (member/group-grant management, replaces the vestigial `StaffOnly`) and `Own` (delete-the-container / grant-Owner). `"staff_only"` has zero call sites outside this one file today, so aliasing it to `Manage` is free. Update `permission_to_action()`: permission names ending `_Add_Owner` map to `Own` (currently `StaffOnly`); names containing `_Manage_` map to `Manage`.

Restore the three currently-inert stubs at the bottom of `authorization.py` — `get_roles_for_permission()`, `role_has_permission()`, `role_has_global_permission()` — ported from `db1932c9e`, retargeted at `Action` instead of `Permissions`. `get_roles_for_permission()` is required by `dojo/group/queries.py` (§7.5) and by the queryset filters below. Also restore `user_is_superuser_or_global_owner()` from `db1932c9e` (checks `user.global_role.role.is_owner`, direct or via group) — it currently hard-returns `user.is_superuser` and is consumed by `IsSuperUserOrGlobalOwner` in `api_permissions.py`.

**Remove two short-circuits in `user_has_permission`:**
```python
if action in {Action.StaffOnly, Action.Delete}:
    return bool(user.is_staff)
```
This resolves *before* object-level checks ever run, which means a Product Owner today can never delete their own Engagement or manage members — it makes Maintainer/Owner powerless under the new model. Route `Delete`/`Manage`/`Own` through the object-level resolver like every other action, with `is_staff` folded in as a bypass inside the `Product_Type`/`Product` branches (where it already lives for other actions).

Leave `user_has_configuration_permission()`'s existing `is_superuser or is_staff → True` bypass **as-is** (a deliberate deviation from the `db1932c9e` pure-`has_perm` version) — reverting it would silently strip config access from every current staff account in a deployment with no global roles assigned yet.

### 7.4 Coexistence with `authorized_users` — union, not replacement, indefinitely

`authorized_users` and the new member/role model both grant, permanently — not as a transitional state. Treat legacy `authorized_users` membership as an implicit grant of exactly `{view, add, edit, import}` (a module constant, e.g. `LEGACY_AUTHORIZED_USERS_ACTIONS`, not literally `Roles.Writer` — no single named role matches those semantics exactly). This reproduces today's actual behavior bit-for-bit: currently a non-staff `authorized_users` member gets `view/add/edit/import` and is denied `delete`/`staff_only` by the short-circuit being removed in §7.3 — mapping to this exact action set means **no existing user gains or loses anything on upgrade**. State this explicitly in the cutover PR description, because "we removed the delete short-circuit" reads as a privilege change until this equivalence is spelled out.

Role grants (direct `Product_Member`/`Product_Type_Member`, group grants via `Product_Group`/`Product_Type_Group` → `Dojo_Group_Member`, plus `Global_Role` on a user or their group) contribute their role's action set. The effective action set for a user on an object is the **union** of the legacy grant (if any) and every role grant that applies. Product inherits Product_Type grants (already true today via `prod_type__authorized_users` folding into `authorized_product_id_set` — the role-aware version must fold `Product_Type_Member`/`Product_Type_Group` the same way).

**Caching:** the current `authorized_product_id_set(user_pk)` / `authorized_product_type_id_set(user_pk)` (`dojo/authorization/query_registrations.py`) are `@cache_for_request`, keyed only on `user_pk` — correct when membership is binary, wrong the instant a Reader is in the "view" set but not "edit". Don't add `action` to the cache key (multiplies queries by distinct actions checked per request, bad on list views). Instead make the cached value **action-aware**: build a `user_pk -> {object_id: frozenset[action]}` map once per request from a fixed small number of queries (direct member rows, group-member rows, legacy `authorized_users`, inherited Product_Type rows, Global_Role), then derive the existing per-action call sites as thin wrappers over that map. The lazy-queryset helpers (`_authorized_product_ids`/`_authorized_product_type_ids`, used so `.filter(id__in=...)` collapses to one SQL subquery) need a separate action-aware `Q`-expansion using `get_roles_for_permission(action)` — and this is precisely where the restored reverse accessors (`product_member__user`, `product_group__group__users`, etc.) from §7.2 are required, not optional.

**Object-level and queryset-level answers must agree** — a user who can `GET /product/5` directly but doesn't see it in the list view (or the reverse) is the classic RBAC bug class. `unittests/test_authorization_queryset_coverage.py` already exists specifically to police this; extend it to run under every flag state in §7.6 rather than writing a new harness.

### 7.5 `dojo/group/` module — restructure to current conventions, don't flat-restore

Doesn't exist today; the `db1932c9e` version was flat (`views.py` 592 lines, `urls.py`, `queries.py`, `utils.py`). Target, per `AGENTS.md`'s canonical module layout:

```
dojo/group/
├── queries.py     # port from db1932c9e, models from dojo.authorization.models, permissions retargeted to Action strings
├── services.py     # get_auth_group_name() and any group-CRUD logic beyond a form save
├── signals.py       # port of db1932c9e's utils.py — it's 4 @receiver handlers, not helpers
├── ui/{forms,views,urls}.py
└── api/{serializer,views,urls}.py
```

`signals.py` is load-bearing, not cosmetic: its `post_save` receiver mirrors each `Dojo_Group` into a real Django `auth.Group` and syncs membership into `auth_group.user_set` — that mirror is what makes `user_has_configuration_permission()` (which falls through to `user.has_perm`) work for group members at all. Port the 999-attempt name-collision loop unchanged, and make sure it's wired from the app's `AppConfig.ready()` (check how `dojo/product/signals.py` is registered and match it) — signal receivers defined but never imported silently don't fire. Preserve the existing guard that skips auto-Owner-assignment when `group.social_provider` is set (dormant today since nothing writes that field without Epic 1.5, but the guard is already correct — keep it for free).

Group management views/routes: with only 6 decorator-based (`@user_is_authorized`) call sites left repo-wide and everything else routed through `url_permissions.py`'s central table, register the new group routes there rather than adding 11 new decorator call sites — keeps the authorization surface auditable in one place, consistent with how every other module in the current codebase is wired.

Forms (`DojoGroupForm`, `Add_Group_MemberForm`, `Add_Product_GroupForm`, `Edit_Product_Group_Form`, `Add_Product_Type_GroupForm`, `Edit_Product_Type_Group_Form`, `GlobalRoleForm`, plus the four `*_MemberForm` variants) are recoverable from `db1932c9e:dojo/forms.py` but land in their owning module's `ui/forms.py` per current convention (group forms in `dojo/group/ui/forms.py`, member forms in `dojo/product/ui/forms.py` / `dojo/product_type/ui/forms.py`), not back in the `dojo/forms.py` monolith they used to share.

### 7.6 Enforcement rollout — three-state flag, dark by default

Mirrors the SSO Epic's own dark-launch pattern (§10, in the master `INTEGRATIONS_ROADMAP.md`; §6.8, in `epic1-sso.md`): `DD_FEATURE_RBAC = off | shadow | on`, read per-call (not bound at import time, or the flag becomes untestable without a process restart).
- **`off`** (the default for PRs 1-6) — today's binary `authorized_users`/`is_staff` logic only, bit-identical to current behavior.
- **`shadow`** — evaluate both the legacy and role-aware resolvers on every check, return the legacy answer, log any divergence at WARNING (`user, object type+pk, permission, legacy_result, rbac_result`). This is the actual value of the dark launch — every access change surfaces before anyone is affected by it. Run a full business cycle before flipping further. Bound the cost if it matters at scale (sampling, or restrict to mutating requests) — negligible at this deployment's current size.
- **`on`** — role-aware resolver is authoritative.

**Current default: `shadow`** (changed from `off` by PR 7, 2026-08-11). It is set in three places, each a fallback for the layer below it not being configured: `DD_FEATURE_RBAC=(str, "shadow")` in `dojo/settings/settings.dist.py` is the last-resort default; `docker-compose.yml`'s `x-rbac-environment` anchor passes `${DD_FEATURE_RBAC:-shadow}` into uwsgi, celerybeat, celeryworker and initializer alike; and the Helm chart's `featureFlags.rbac` value (default `shadow`, rendered by the `defectdojo.featureFlagsEnv` helper into the same four workloads) does the same for Kubernetes. The flag must reach the celery containers, not just the web tier — tasks resolve permissions too, and a split-brain flag would make the workers disagree with the web pods about who can see what. The Helm helper rejects any value outside `off|shadow|on` at render time, deliberately, because the settings module fails *closed* to `off` and a typo would otherwise quietly disable enforcement on an instance the operator believes is enforcing.

### 7.7 REST API

12 routes to restore (not 8 — v3 introduced `asset`/`organization` aliases over the same `Product`/`Product_Type` tables, confirmed via removal comments in `dojo/asset/api/urls.py` and `dojo/organization/api/urls.py`): `dojo_groups`, `dojo_group_members`, `roles`, `global_roles`, `product_members`, `product_groups`, `asset_members`, `asset_groups`, `product_type_members`, `product_type_groups`, `organization_members`, `organization_groups`. Each lands in its owning module's `api/` package (`dojo/group/api/`, `dojo/authorization/api/`, `dojo/product/api/`, `dojo/product_type/api/`, `dojo/asset/api/`, `dojo/organization/api/`) rather than the old monolithic `dojo/api_v2/{views,serializers}.py` — verify exact route/basename strings against the removal comments before wiring, since AGENTS.md flags that route and basename often differ and either breaking silently breaks URL reversing.

Serializers and the 6 deleted `UserHas*Permission` DRF permission classes port near-verbatim from `db1932c9e:dojo/api_v2/{serializers,permissions}.py`, with `Permissions.X` references swapped to the corresponding `Action` string. Preserve `validate()` bodies wholesale (duplicate-row guard, "at least one Owner" invariant — the only enforcement of those rules absent DB constraints per §7.2). Every member/group viewset disables `PATCH` (object authorization can't evaluate a partial payload) and `ProductTypeMemberViewSet.destroy` must keep refusing to remove the last Owner. `GlobalRoleViewSet` stays superuser-only — it's the actual privilege-escalation surface of the whole system.

### 7.8 Frontend

The UI is genuinely additive — confirmed from the code, not inferred: `view_product_details.html` and `view_product_type.html` (both the Tailwind and classic template trees) already have empty, pre-wired override blocks — `{% block rbac_members_panel %}{% endblock %}` and `{% block rbac_groups_panel %}{% endblock %}` — placed immediately after the existing `authorized_users_panel` block closes, plus a `{% block groups_submenu_link %}{% endblock %}` stub in the sidebar's Users flyout with the comment "Pro restores the Groups link by overriding this block." Build into these; don't restructure the surrounding templates.

**Recommendation: the new panels sit alongside Authorized Users, not replacing it** — flat full-access grants remain the simple case for "just add this one person," role-based grants (individual or group) are for organizing access at scale. The blocks were already scaffolded that way. Revisit merging them only after role-based access has been in use long enough to judge whether Authorized Users has become fully redundant — not on day one.

Two template trees exist and both must ship (`dojo/templates/dojo/` — Tailwind, and `dojo/templates_classic/dojo/` — Bootstrap 3, selected per-user by `dojo/template_loaders.py`'s `UIPreferenceLoader`, classic is the default). This is cheaper than it sounds: the "Tailwind" tree's compiled CSS (`components/tailwind.css`) re-implements Bootstrap-shaped class names (`.btn`, `.panel`, `.dropdown-menu`, `.table`, `.modal`) via `@apply`, so the two trees are near-identical markup today, not a parallel design effort.

New templates (flat under `dojo/templates/dojo/` and its classic twin, matching how `new_product_authorized_users.html` already sits despite its view living in `dojo/product/ui/views.py`): `dojo_groups.html` (list, clone `users.html`'s filter/paging/sort scaffolding), `dojo_group_add.html`/`dojo_group_edit.html` (plain textarea, no markdown editor — nothing else in the current admin forms wires one up for a new form), `view_dojo_group.html` (description + members-with-role table + granted-products/-product-types table), `delete_dojo_group.html` (clone `delete_product_type.html`'s danger-zone shell), `new_dojo_group_member.html`. For the Product/Product Type panels, a single shared partial (e.g. `_rbac_grant_panel.html`, parameterized by grants/add-url/label/entity-field) covers all four combinations (product×group, product×member, product-type×group, product-type×member) rather than duplicating ~40 lines four times — the one place in this plan a shared partial clearly earns its cost.

Interaction conventions, matched to what's actually live in this codebase (not invented): row-level remove/role-change actions use htmx (`hx-post`/`hx-confirm`/`hx-swap="none"`, CSRF already free via `body`'s `hx-headers` in `base.html`) — the newer, less-markup idiom the codebase is already moving toward (see `view_finding.html`'s unlink-JIRA button), used deliberately in preference to the Authorized Users panel's older hidden-form-per-row pattern; dropdown menus stay on Bootstrap's `data-toggle="dropdown"` (global JS already wired, don't mix in Alpine per-row — that's reserved for sidebar/nav-level disclosures in this codebase); no dark-mode handling anywhere (confirmed zero `prefers-color-scheme`/`data-theme` in the compiled CSS — don't add it here inconsistently with everything else). Copy strings go through label-constant modules in the style of `dojo/organization/labels.py`'s existing `ORG_USERS_LABEL`/`ORG_USERS_ADD_LABEL`/etc. — add parallel `ORG_MEMBERS_*`/`ORG_GROUPS_*` keys rather than hardcoding "Product Type" strings, keeping the v3 Organization/Asset vocabulary consistent with how the rest of the current UI already talks about these objects even though the underlying template/view files still say Product/Product Type internally.

### 7.9 SSO interaction

**No functional change needed in `dojo/user/social_pipeline.py`** — verified by re-reading all four pipeline steps against this design. `enforce_zero_privilege_defaults` only clears `is_superuser`/`is_staff` on new users and creates no `Product_Member`/`Dojo_Group_Member`/`Global_Role` rows; under the new resolver that's *more* correct than before, not less — a JIT-provisioned user with zero grants anywhere resolves to zero access, same intent as today, just precisely modeled instead of implicit. No pipeline step touches `System_Settings.default_group*` (confirmed dropped for real in §7.2) or `Dojo_Group.social_provider` (stays dormant — reactivated as a column, written by nothing, which is exactly the correct state while Epic 1.5 stays deferred). Update two pieces of now-stale documentation as part of this epic, not the pipeline logic itself: the docstrings in `social_pipeline.py` that tell the operator to grant access "through the Authorized Users panels" (name the new group/role panels too), and this file's own §2 reasoning about Pro-coexistence (superseded, see the note at the top of §2, in the master `INTEGRATIONS_ROADMAP.md`).

### 7.10 PR sequence

Seven PRs, first four fully dark (`DD_FEATURE_RBAC=off`, zero observable change):

| # | Content | Layer | Dark? |
|---|---|---|---|
| 1 | Migration `0278` + `related_name` restoration + companion accessor restoration | Backend | Yes |
| 2 | Fixed `roles_permissions.py` matrix + `Action.Manage`/`Action.Own` + restored stubs (`get_roles_for_permission`, `role_has_permission`, `role_has_global_permission`, `user_is_superuser_or_global_owner`) — pure functions, unit-tested in isolation, not yet consulted | Backend | Yes |
| 3 | Role-aware resolver (§7.4), action-aware caching, `DD_FEATURE_RBAC` flag + dispatcher, coexistence test matrix | Backend | Yes (ships `off`) |
| 4 | `dojo/group/` module (§7.5) — routes registered but flag-gated | Backend | Yes |
| 5 | REST API — 12 routes (§7.7) | Backend | Yes |
| 6 | Frontend panels (§7.8) — both template trees | Frontend | Yes (panels render nothing meaningful until flag is `on`, or gate rendering on the flag too) |
| 7 | Default flipped to `shadow` + env/Helm knob (§7.6); `docs/content/admin/user_management/` and `social_pipeline.py` docstrings de-staled; §2 and §9 bookkeeping. **Landed 2026-08-11.** Remaining and deliberately manual: review the divergence log for a business cycle, flip to `on`, retire the flag after a bake period | Operator | No |

Migration `0279` (uniqueness constraints, §7.2) can land any time after PR 1, independently. (See the renumbering note in §7.2 above — this became `0280` in practice.)

### 7.11 Risk register additions (folds into §9, in `risk-register.md`)

| ID | Risk | Mitigation |
|---|---|---|
| **R13** | Broken role→action matrix ships as "fixed" but Reader still gets `add`, or Writer still gets `delete` — the single most likely way this epic ships a security regression, since the current matrix *looks* usable already. | PR 2 is isolated specifically so it gets its own focused review against the corrected table in §7.3. Explicit test: Reader's action set is exactly `{view}`. |
| **R14** | Object-level and queryset-level authorization answers diverge post-cutover (visible-but-403, or invisible-but-fetchable rows). | Both derive from the same resolver by construction (§7.4). `test_authorization_queryset_coverage.py` extended to run under all three flag states; shadow mode surfaces divergence before cutover. |
| **R15** | Duplicate membership rows (no DB uniqueness, §7.2) — highest role silently wins if it happens. | Form-level `clean()` uniqueness in PR 4/5. Migration `0279` closes the gap once real usage exists to dedupe against. |
| **R16** | `auth.Group` mirror signal not wired into `AppConfig.ready()` — fails silently as *missing* config permissions for group members, not an error. | PR 4 includes an explicit test: creating a `Dojo_Group_Member` adds the user to the mirrored `auth.Group`. |
| **R17** | Fixture-seeded `product_type_member` row (admin as Owner of Product Type 1) activates functionally on cutover. | Called out explicitly in PR 7's description. Harmless (admin is already superuser) but every fresh install reproduces it — a deliberate decision to keep or remove from `dojo/fixtures/product_type.json`, not an oversight. |
| **R18** | Upstream merge conflict — `dojo/authorization/` is upstream-owned and actively churning; this epic diverges the fork meaningfully. | New logic lands in new files (`dojo/group/`, restored stubs, matrix fix) rather than rewriting `authorization.py` wholesale; keeps the conflict surface small. |

**Done 2026-08-11 (PR 7):** R1 is marked closed in §9 (in `risk-register.md`) and R12 has been retired from the active register there, with the reasoning kept as a note beneath the table.
