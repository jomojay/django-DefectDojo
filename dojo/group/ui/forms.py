"""
Group and group-membership forms.

Ported from the pre-3.0 ``dojo/forms.py`` (commit ``db1932c9e``) with only the
import paths adapted: the models now live in ``dojo.authorization.models`` and
``get_group_member_roles`` in this module's own ``queries.py``. Per current
convention (AGENTS.md Phase 3) they land here rather than back in the
``dojo/forms.py`` monolith, so there is no re-export to add.
"""
from django import forms
from django.db.models import Q

from dojo.authorization.models import Dojo_Group, Dojo_Group_Member
from dojo.group.queries import get_group_member_roles
from dojo.models import Dojo_User


class DojoGroupForm(forms.ModelForm):

    name = forms.CharField(max_length=255, required=True)
    description = forms.CharField(widget=forms.Textarea(attrs={}), required=False)

    class Meta:
        model = Dojo_Group
        fields = ["name", "description"]
        exclude = ["users"]


class DeleteGroupForm(forms.ModelForm):
    id = forms.IntegerField(required=True,
                            widget=forms.widgets.HiddenInput())

    class Meta:
        model = Dojo_Group
        fields = ["id"]


class Add_Group_MemberForm(forms.ModelForm):

    """
    Multi-user picker for one group and one role.

    The user queryset deliberately excludes superusers (they already have
    everything), inactive users, and anybody already in the group — the last of
    which is the only guard against duplicate membership rows until migration
    ``0279`` adds a real uniqueness constraint (INTEGRATIONS_ROADMAP.md R15).
    """

    users = forms.ModelMultipleChoiceField(queryset=Dojo_Group_Member.objects.none(), required=True, label="Users")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["group"].disabled = True
        current_members = Dojo_Group_Member.objects.filter(group=self.initial["group"]).values_list("user", flat=True)
        self.fields["users"].queryset = Dojo_User.objects.exclude(
            Q(is_superuser=True)
            | Q(id__in=current_members)).exclude(is_active=False).order_by("first_name", "last_name")
        self.fields["role"].queryset = get_group_member_roles()

    class Meta:
        model = Dojo_Group_Member
        fields = ["group", "users", "role"]


class Edit_Group_MemberForm(forms.ModelForm):

    """One existing membership row; only the role is editable."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["group"].disabled = True
        self.fields["user"].disabled = True
        self.fields["role"].queryset = get_group_member_roles()

    class Meta:
        model = Dojo_Group_Member
        fields = ["group", "user", "role"]


class Delete_Group_MemberForm(Edit_Group_MemberForm):

    """Read-only variant of the above, backing the confirm-removal page."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].disabled = True
