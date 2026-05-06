from django import forms
from core.upload_validation import MIGRATION_EXTENSIONS, validate_uploaded_file
from .models import ImportSession

class MigrationUploadForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['source_system'].choices = [
            (ImportSession.SOURCE_TALLY_EXCEL, 'Tally Excel/CSV Export'),
            (ImportSession.SOURCE_GENERIC_EXCEL, 'Generic Excel'),
            (ImportSession.SOURCE_GENERIC_CSV, 'Generic CSV'),
        ]

    class Meta:
        model = ImportSession
        fields = [
            'source_system',
            'sync_mode',
            'source_company_guid',
            'source_period_start',
            'source_period_end',
            'file',
        ]
        widgets = {
            'source_period_start': forms.DateInput(attrs={'type': 'date'}),
            'source_period_end': forms.DateInput(attrs={'type': 'date'}),
        }
        help_texts = {
            'source_company_guid': 'Optional Tally company GUID or client code used to identify repeat syncs.',
            'sync_mode': 'Use replace-period only after reviewing duplicates and period boundaries.',
        }

    def clean_file(self):
        file_obj = self.cleaned_data.get("file")
        try:
            return validate_uploaded_file(
                file_obj,
                allowed_extensions=MIGRATION_EXTENSIONS,
                max_mb=20,
                require_signature=False,
            )
        except Exception as exc:
            raise forms.ValidationError(str(exc))

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get('source_period_start')
        end = cleaned.get('source_period_end')
        if start and end and start > end:
            raise forms.ValidationError("Source period start cannot be after source period end.")
        return cleaned
