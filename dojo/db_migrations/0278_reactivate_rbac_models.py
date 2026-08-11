"""
Reactivate the seven RBAC tables plus Dojo_Group as OS-owned, managed=True models.

Migration 0268 flipped these eight tables to managed=False and dropped their reverse
accessors state-only (SeparateDatabaseAndState, empty database_operations) when the
authorization layer was handed to a "pro" package. That package will never be installed
in this deployment (see INTEGRATIONS_ROADMAP.md §7) - the reservation these tables were
shelved for doesn't apply, so this migration reclaims them.

Every operation here is state-only and issues zero DDL:

* AlterModelOptions dropping managed=False - verified with `sqlmigrate dojo 0278`
  emitting no statements. The tables already exist with matching schema (created by
  pre-3.0 migrations, never dropped), so there is nothing for Django to create.
* related_name restoration on the model fields themselves (dojo/authorization/models.py,
  dojo/product/models.py, dojo/product_type/models.py) - these fields carried
  related_name="+" purely to avoid clashing with the never-arriving pro package's own
  accessors. Restoring the pre-3.0 names (e.g. user.global_role, product.product_members)
  is required for the role-aware authorization engine landing in a later PR, which reads
  them directly.
* Re-adding the members/authorization_groups M2M accessors on Product/Product_Type that
  0268 removed - they are convenience accessors over the Product_Member/Product_Group
  (and Product_Type equivalent) through-tables, which were never dropped.

AlterModelTable is deliberately NOT repeated here - 0268 already pinned every db_table
and re-issuing it is a real (if no-op) DDL path worth avoiding.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dojo", "0277_seed_deduplication_execution_mode"),
    ]

    operations = [
        migrations.AlterModelOptions(name="dojo_group", options={}),
        migrations.AlterModelOptions(name="dojo_group_member", options={}),
        migrations.AlterModelOptions(name="global_role", options={}),
        migrations.AlterModelOptions(name="product_group", options={}),
        migrations.AlterModelOptions(name="product_member", options={}),
        migrations.AlterModelOptions(name="product_type_group", options={}),
        migrations.AlterModelOptions(name="product_type_member", options={}),
        migrations.AlterModelOptions(name="role", options={"ordering": ("name",)}),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="product",
                    name="members",
                    field=models.ManyToManyField(blank=True, related_name="product_members", through="dojo.Product_Member", to="dojo.dojo_user"),
                ),
                migrations.AddField(
                    model_name="product",
                    name="authorization_groups",
                    field=models.ManyToManyField(blank=True, related_name="product_groups", through="dojo.Product_Group", to="dojo.dojo_group"),
                ),
                migrations.AddField(
                    model_name="product_type",
                    name="members",
                    field=models.ManyToManyField(blank=True, related_name="prod_type_members", through="dojo.Product_Type_Member", to="dojo.dojo_user"),
                ),
                migrations.AddField(
                    model_name="product_type",
                    name="authorization_groups",
                    field=models.ManyToManyField(blank=True, related_name="product_type_groups", through="dojo.Product_Type_Group", to="dojo.dojo_group"),
                ),
            ],
            database_operations=[],
        ),
    ]
