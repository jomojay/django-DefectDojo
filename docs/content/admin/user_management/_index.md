---
title: "User Management"
description: "Manage users, access control, and authentication in DefectDojo"
summary: ""
date: 2023-09-07T16:06:50+02:00
lastmod: 2023-09-07T16:06:50+02:00
draft: false
weight: 5
chapter: true
seo:
  title: "" # custom title (optional)
  description: "" # custom description (recommended)
  canonical: "" # custom canonical URL (optional)
  robots: "" # custom robot tags (optional)
exclude_search: true
---

This installation has **two access-control models running side by side**, and a user's access is the union of whatever both of them grant. Neither replaces the other.

## Authorized Users

The simplest grant, and the one that has always been available in open-source DefectDojo: a user is given access to a Product or a Product Type by being added to that record's Authorized Users list. It is all-or-nothing — there is no read-only tier — which is the reason the role-based model below was brought back. Superusers and staff can see everything regardless.

* [Authorized Users](./os__authorized_users/) — how to grant access to Products and Product Types

## Roles, Members and Groups

Upstream open-source DefectDojo dropped Members / Groups / Global Roles at the 3.0 release and left them to DefectDojo Pro. **This installation reactivates them**, so a user can be granted a specific Role (Reader, Writer, Maintainer, Owner, API Importer) on a Product or Product Type rather than blanket access — individually, or through a Group.

Enforcement is staged behind the `DD_FEATURE_RBAC` setting. In `shadow` — the default — the panels and the roles are all present and assignable, and every decision the roles would make is written to the log, but the Authorized Users answer is still the one that takes effect. Set `DD_FEATURE_RBAC=on` to make roles authoritative.

* [Permissions in DefectDojo](./about_perms_and_roles/) — overview of Roles, Memberships, Global Roles, and Configuration Permissions
* [Set a User's Permissions](./set_user_permissions/) — assigning Roles, Global Roles, and Configuration Permissions
* [Share permissions: User Groups](./create_user_group/) — assigning permissions to many users at once
* [Action permission charts](./user_permission_chart/) — full reference of every permission for every Role

## Authentication

Local username/password plus the password-reset flow, and — where the deployment configures it — OIDC single sign-on against Microsoft Entra ID. Signing in through the identity provider grants no access on its own: a new account is provisioned with zero privileges and is then granted access through the panels above.

## DefectDojo Pro

* [Set Permissions in Pro](./pro_permissions_overhaul/) — Pro-specific UI for managing Members and Permissions
* [Single Sign-On](/admin/sso/) — SAML and OAuth setup for Pro

## Migrating between editions

If you're moving from open-source's Authorized Users to Pro's RBAC, or upgrading from a pre-3.0 open-source release that used RBAC into the current Authorized Users model, see the [3.0 upgrade notes](/releases/os_upgrading/3.0/#authorized-users-panel-replaces-membersgroups-under-legacy-authorization). Existing access is preserved automatically.
