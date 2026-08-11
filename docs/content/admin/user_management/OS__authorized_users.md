---
title: "Open-Source Permissions"
description: "How access to Products and Product Types is granted in open-source DefectDojo"
weight: 1
audience: opensource
---

Open-source DefectDojo controls access to Products and Product Types with the **Authorized Users** model. Each Product and Product Type has an Authorized Users panel listing the people who can see that record and the data nested under it.

> **This installation also has Roles, Members and Groups.** The Authorized Users model described here is still live and still grants exactly what this page says — but it is no longer the only way to grant access. This installation reactivates the role-based system covered in [Permissions in DefectDojo](../about_perms_and_roles/), and a user's access is the **union** of their Authorized Users entries and their role grants. Read this page for the flat, all-or-nothing grant; read that one for graduated Reader/Writer/Maintainer/Owner access.

## How access is granted

There are two lists, and a user only needs to appear on one of them to gain access:

- **A Product's Authorized Users list** grants access to that single Product, plus everything nested underneath it (its Engagements, Tests, Findings, and Endpoints).
- **A Product Type's Authorized Users list** grants access to the Product Type itself **and cascades to every Product underneath it**. A user who is authorized on a Product Type does not need to also be added to each child Product — they are already covered.

Within the Authorized Users model itself there is no gradation: a user is either on the list (or is a superuser/staff member — see below), or they cannot see the Product. Everyone on the list gets the same level of access, and it includes editing and importing, not just reading. If you need a read-only tier, or a distinction between the people who can delete records and the people who cannot, that is what the [Roles, Members and Groups](../about_perms_and_roles/) model is for — grants made there add to whatever the two lists above already give.

## Superusers and staff bypass the lists

Users marked as **superuser** or **staff** in DefectDojo can see and act on every Product and Product Type regardless of the Authorized Users lists. The lists exist to grant access to non-staff users; they do not restrict staff or superusers.

The first account created on a fresh DefectDojo installation is automatically a superuser.

## Who can edit the lists

Only **superuser** or **staff** users see the controls to add or remove people from an Authorized Users panel. Everyone else who has access to a Product or Product Type sees the panel as a read-only roster — useful for finding out who else is on the team, but not for changing membership.

## Where the panel lives

The Authorized Users panel appears on two pages in the classic UI:

- The **Product detail page** has an Authorized Users panel for that Product. It supports two actions for staff users:
  - **Add a user to the Product's Authorized Users list**
  - **Remove a user from the Product's Authorized Users list**
- The **Product Type detail page** has an Authorized Users panel for that Product Type, with the corresponding two actions:
  - **Add a user to the Product Type's Authorized Users list**
  - **Remove a user from the Product Type's Authorized Users list**

When you remove a user from a Product Type's list, the cascade is removed too — they lose access to every child Product unless they're still on a specific Product's list, they hold a role grant on the record (see [Permissions in DefectDojo](../about_perms_and_roles/)), or they're a staff/superuser.

The Members and Groups panels for role-based grants sit alongside the Authorized Users panel on both of these pages. Removing someone from an Authorized Users list does not remove any role grant they hold, and vice versa — the two are tracked separately.

## Choosing between Product and Product Type access

A few rules of thumb:

- If a person should see every Product under a category (for example, every Product owned by a particular team), put them on the **Product Type** list and let the cascade take care of the rest.
- If a person should only see one specific Product, put them on that **Product**'s list.
- If you find yourself adding the same person to many individual Products under one Product Type, that's a signal you should add them to the Product Type instead.

## Coming from a previous version of DefectDojo

DefectDojo open-source moved back to the Authorized Users model in version 3.0. If you're upgrading from a release that had the Members / Groups / Global Roles system, your existing access is carried forward into Authorized Users automatically by the upgrade — no manual mapping is needed.

The upgrade ships with a read-only management command, `preview_legacy_authorization_migration`, that summarizes what an upgrade would change against a copy of your database. The recommended workflow is to install 3.0 in a staging environment with a snapshot of production, run the command, review the summary, and then upgrade production.

If you're moving the other direction — from open-source to DefectDojo Pro — Pro ships a `reconcile_authorized_users_to_rbac` command that brings Authorized Users access forward into Pro's RBAC. It supports `--dry-run` and is idempotent.

For more detail on both paths, see the [3.0 upgrade notes](/releases/os_upgrading/3.0/#authorized-users-panel-replaces-membersgroups-under-legacy-authorization).
