from django import forms

from .models import AgentConfig, Lead


class StyledMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.Textarea):
                field.widget.attrs.setdefault("rows", 4)


class ConfigForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = AgentConfig
        exclude = ["discovery_cursor", "gigs_checked_at", "remotive_checked_at"]


class LeadForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = Lead
        fields = ["name", "market", "category", "city", "website", "email", "phone", "address", "notes"]
        help_texts = {"market": "Sets their time zone and phone format. Leave empty for Nigeria."}


class LeadContactForm(forms.ModelForm):
    class Meta:
        model = Lead
        fields = ["email", "phone", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}
