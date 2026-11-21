from datetime import timedelta

from django import forms
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.urls import reverse
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from eventyay.base.models import CachedFile

RETAINED_UPLOAD_SUFFIX = "-retained"
RETAINED_UPLOAD_DAYS = 1


def retained_upload_url(cached_file):
    return reverse("cachedfile.download", kwargs={"id": cached_file.id})


class RetainedFileInput(forms.ClearableFileInput):
    """A file input that also carries a file kept from a failed submit, so it is not lost."""

    template_name = "exhibitors/widgets/retained_file_input.html"

    def __init__(self, attrs=None, retained=None):
        super().__init__(attrs)
        self.retained = retained

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        context["widget"]["retained"] = self.retained
        context["widget"]["retained_name"] = name + RETAINED_UPLOAD_SUFFIX
        context["widget"]["retained_url"] = retained_upload_url(self.retained)
        return context


class RetainedUploadsMixin:
    """Keep the files of a failed submit and put them back on the next one, as browsers cannot refill file inputs."""

    def dispatch(self, request, *args, **kwargs):
        try:
            return super().dispatch(request, *args, **kwargs)
        finally:
            self.close_restored_uploads()

    def close_restored_uploads(self):
        for stream in getattr(self, "restored_upload_streams", ()):
            stream.close()
        self.restored_upload_streams = []

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        if form.is_bound:
            self.restore_retained_uploads(form)
        return form

    def form_invalid(self, form):
        self.retain_uploads(form)
        return super().form_invalid(form)

    def retained_upload_session_key(self):
        session = self.request.session
        if not getattr(session, "session_key", None) and hasattr(session, "save"):
            session.save()
            session.modified = True
        return getattr(session, "session_key", None)

    def find_retained_upload(self, cached_file_id):
        """The kept file of this session, whether or not it is still usable; other sessions' files are never found."""
        session_key = self.retained_upload_session_key()
        if not session_key:
            return None
        try:
            return CachedFile.objects.filter(id=cached_file_id, session_key=session_key).first()
        except (ValueError, ValidationError):
            return None

    def restore_retained_uploads(self, form):
        files = form.files.copy()
        unavailable = []
        for name, field in form.fields.items():
            if not isinstance(field, forms.FileField):
                continue
            key = form.add_prefix(name)
            cached_file_id = form.data.get(key + RETAINED_UPLOAD_SUFFIX)
            if not cached_file_id or files.get(key) or form.data.get(f"{key}-clear"):
                continue
            cached_file = self.find_retained_upload(cached_file_id)
            if cached_file is None:
                continue
            expired = cached_file.expires is not None and cached_file.expires <= now()
            if expired or not cached_file.file or not cached_file.file.storage.exists(cached_file.file.name):
                unavailable.append(name)
                continue
            stream = cached_file.file.storage.open(cached_file.file.name, "rb")
            self.restored_upload_streams = [*getattr(self, "restored_upload_streams", ()), stream]
            upload = UploadedFile(
                file=stream,
                name=cached_file.filename,
                content_type=cached_file.type,
                size=cached_file.file.size,
            )
            upload.retained_from = cached_file
            files[key] = upload
        form.files = files
        if unavailable:
            self.require_new_selection(form, unavailable)

    def require_new_selection(self, form, names):
        full_clean = form.full_clean

        def full_clean_requiring_new_selection():
            full_clean()
            for name in names:
                if name in form.fields and name not in form.errors:
                    form.add_error(name, _("The file you chose before is no longer available. Please choose it again."))

        form.full_clean = full_clean_requiring_new_selection

    def cache_upload(self, upload):
        session_key = self.retained_upload_session_key()
        if not session_key:
            return None
        cached_file = CachedFile.objects.create(
            expires=now() + timedelta(days=RETAINED_UPLOAD_DAYS),
            date=now(),
            web_download=True,
            filename=upload.name,
            type=upload.content_type or "application/octet-stream",
            session_key=session_key,
        )
        upload.seek(0)
        cached_file.file.save(upload.name, upload, save=True)
        return cached_file

    def retain_uploads(self, form):
        form.retained_upload_urls = {}
        for name, field in form.fields.items():
            if not isinstance(field, forms.FileField) or name in form.errors:
                continue
            upload = form.files.get(form.add_prefix(name))
            if not isinstance(upload, UploadedFile):
                continue
            cached_file = getattr(upload, "retained_from", None) or self.cache_upload(upload)
            if cached_file is None:
                continue
            widget = RetainedFileInput(attrs=field.widget.attrs, retained=cached_file)
            widget.is_required = field.widget.is_required
            field.widget = widget
            form.retained_upload_urls[name] = retained_upload_url(cached_file)
