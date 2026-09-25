from django import forms
from django.utils import timezone

from .models import Garden, Trough, UnloadHandoff, WitherBatch


class GardenForm(forms.ModelForm):
    class Meta:
        model = Garden
        fields = ["name", "altitudeBand", "notes"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "input"}),
            "altitudeBand": forms.TextInput(attrs={"class": "input"}),
            "notes": forms.Textarea(attrs={"class": "input", "rows": 3}),
        }


class TroughForm(forms.ModelForm):
    class Meta:
        model = Trough
        fields = ["garden", "troughCode", "cultivar", "loadKg", "status"]
        widgets = {
            "garden": forms.Select(attrs={"class": "input"}),
            "troughCode": forms.TextInput(attrs={"class": "input"}),
            "cultivar": forms.TextInput(attrs={"class": "input"}),
            "loadKg": forms.NumberInput(attrs={"class": "input", "step": "0.01"}),
            "status": forms.Select(attrs={"class": "input"}),
        }


class WitherBatchForm(forms.ModelForm):
    class Meta:
        model = WitherBatch
        fields = [
            "trough",
            "startedAt",
            "targetMoisture",
            "actualMoisture",
            "rollGrade",
        ]
        widgets = {
            "trough": forms.Select(attrs={"class": "input"}),
            "startedAt": forms.DateTimeInput(
                attrs={"class": "input", "type": "datetime-local"},
                format="%Y-%m-%dT%H:%M",
            ),
            "targetMoisture": forms.NumberInput(
                attrs={"class": "input", "step": "0.01"}
            ),
            "actualMoisture": forms.NumberInput(
                attrs={"class": "input", "step": "0.01"}
            ),
            "rollGrade": forms.TextInput(attrs={"class": "input"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["startedAt"].input_formats = [
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
        ]
        if self.instance and self.instance.pk and self.instance.startedAt:
            local = timezone.localtime(self.instance.startedAt)
            self.initial["startedAt"] = local.strftime("%Y-%m-%dT%H:%M")


class UnloadHandoffForm(forms.ModelForm):
    class Meta:
        model = UnloadHandoff
        fields = [
            "trough",
            "handoffAt",
            "receivingTeam",
            "outKg",
            "signer",
        ]
        widgets = {
            "trough": forms.Select(attrs={"class": "input"}),
            "handoffAt": forms.DateTimeInput(
                attrs={"class": "input", "type": "datetime-local"},
                format="%Y-%m-%dT%H:%M",
            ),
            "receivingTeam": forms.TextInput(attrs={"class": "input"}),
            "outKg": forms.NumberInput(attrs={"class": "input", "step": "0.01"}),
            "signer": forms.TextInput(attrs={"class": "input"}),
        }
        help_texts = {
            "trough": "仅「可下槽」槽位可开卷；同槽有未完成卷时不可再开。",
            "outKg": "须为正数，且不得超过该槽装叶量。",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["handoffAt"].input_formats = [
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
        ]
        if not self.initial.get("handoffAt"):
            self.initial["handoffAt"] = timezone.localtime(timezone.now()).strftime(
                "%Y-%m-%dT%H:%M"
            )
        # 开卷只允许选择可下槽槽位。
        self.fields["trough"].queryset = (
            Trough.objects.filter(status=Trough.STATUS_READY)
            .select_related("garden")
            .order_by("garden__name", "troughCode")
        )
