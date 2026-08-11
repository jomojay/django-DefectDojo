"""
Role-based access control models: ``Dojo_Group`` plus the seven RBAC classes below it.

These tables were shelved as ``managed=False`` shells at the OS 3.0 split
(migration ``0268_release_authorization_to_pro``), reserved for a ``pro``
package that this deployment will never install. Migration
``0278_reactivate_rbac_models`` reclaimed ownership: the tables are
``managed=True`` again and every FK/M2M carries its real reverse accessor
(no more ``related_name="+"``), so this module is the canonical, live
owner of role-based authorization data. See INTEGRATIONS_ROADMAP.md §7 for
the full reactivation spec, including how these coexist permanently with
the legacy ``authorized_users`` M2M on ``Product``/``Product_Type`` rather
than replacing it.
"""

from django.contrib.auth.models import Group
from django.db import models
from django.utils.translation import gettext_lazy as _


class Dojo_Group(models.Model):

    AZURE = "AzureAD"
    REMOTE = "Remote"
    SOCIAL_CHOICES = (
        (AZURE, _("AzureAD")),
        (REMOTE, _("Remote")),
    )
    name = models.CharField(max_length=255, unique=True)
    description = models.CharField(max_length=4000, null=True, blank=True)
    users = models.ManyToManyField(
        "dojo.Dojo_User",
        through="dojo.Dojo_Group_Member",
        related_name="users",
        blank=True,
    )
    auth_group = models.ForeignKey(Group, null=True, blank=True, on_delete=models.CASCADE)
    social_provider = models.CharField(
        max_length=10,
        choices=SOCIAL_CHOICES,
        blank=True,
        null=True,
        help_text=_("Group imported from a social provider."),
        verbose_name=_("Social Authentication Provider"),
    )

    class Meta:
        app_label = "dojo"
        db_table = "dojo_dojo_group"

    def __str__(self):
        return self.name


class Role(models.Model):
    name = models.CharField(max_length=255, unique=True)
    is_owner = models.BooleanField(default=False)

    class Meta:
        app_label = "dojo"
        db_table = "dojo_role"
        ordering = ("name",)

    def __str__(self):
        return self.name


class Dojo_Group_Member(models.Model):
    group = models.ForeignKey("dojo.Dojo_Group", on_delete=models.CASCADE)
    user = models.ForeignKey("dojo.Dojo_User", on_delete=models.CASCADE)
    role = models.ForeignKey(
        Role,
        on_delete=models.CASCADE,
        help_text=_("This role determines the permissions of the user to manage the group."),
        verbose_name=_("Group role"),
    )

    class Meta:
        app_label = "dojo"
        db_table = "dojo_dojo_group_member"


class Global_Role(models.Model):
    user = models.OneToOneField("dojo.Dojo_User", null=True, blank=True, on_delete=models.CASCADE)
    group = models.OneToOneField("dojo.Dojo_Group", null=True, blank=True, on_delete=models.CASCADE)
    role = models.ForeignKey(
        Role,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        help_text=_("The global role will be applied to all product types and products."),
        verbose_name=_("Global role"),
    )

    class Meta:
        app_label = "dojo"
        db_table = "dojo_global_role"


class Product_Member(models.Model):
    product = models.ForeignKey("dojo.Product", on_delete=models.CASCADE)
    user = models.ForeignKey("dojo.Dojo_User", on_delete=models.CASCADE)
    role = models.ForeignKey(Role, on_delete=models.CASCADE)

    class Meta:
        app_label = "dojo"
        db_table = "dojo_product_member"


class Product_Group(models.Model):
    product = models.ForeignKey("dojo.Product", on_delete=models.CASCADE)
    group = models.ForeignKey("dojo.Dojo_Group", on_delete=models.CASCADE)
    role = models.ForeignKey(Role, on_delete=models.CASCADE)

    class Meta:
        app_label = "dojo"
        db_table = "dojo_product_group"


class Product_Type_Member(models.Model):
    product_type = models.ForeignKey("dojo.Product_Type", on_delete=models.CASCADE)
    user = models.ForeignKey("dojo.Dojo_User", on_delete=models.CASCADE)
    role = models.ForeignKey(Role, on_delete=models.CASCADE)

    class Meta:
        app_label = "dojo"
        db_table = "dojo_product_type_member"


class Product_Type_Group(models.Model):
    product_type = models.ForeignKey("dojo.Product_Type", on_delete=models.CASCADE)
    group = models.ForeignKey("dojo.Dojo_Group", on_delete=models.CASCADE)
    role = models.ForeignKey(Role, on_delete=models.CASCADE)

    class Meta:
        app_label = "dojo"
        db_table = "dojo_product_type_group"
