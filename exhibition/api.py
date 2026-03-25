from django.conf import settings
from django.utils import timezone
from eventyay.api.serializers.i18n import I18nAwareModelSerializer
from eventyay.base.models import OrderPosition
from i18nfield.strings import LazyI18nString
from rest_framework import status, views, viewsets
from rest_framework.response import Response

from .models import ExhibitorInfo, ExhibitorSettings, ExhibitorTag, Lead


def _localize_i18n_value(value, locale):
    if isinstance(value, LazyI18nString):
        return value.localize(locale)
    return value


def _get_exhibitor_locale(exhibitor):
    event = getattr(exhibitor, 'event', None)
    return getattr(event, 'locale', None) or settings.LANGUAGE_CODE


class ExhibitorAuthView(views.APIView):
    def post(self, request, *args, **kwargs):
        key = request.data.get('key')

        if not key:
            return Response(
                {'detail': 'Missing parameters'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            exhibitor = ExhibitorInfo.objects.get(key=key)
            locale = _get_exhibitor_locale(exhibitor)
            return Response(
                {
                    'success': True,
                    'exhibitor_id': exhibitor.id,
                    'exhibitor_name': _localize_i18n_value(exhibitor.name, locale),
                    'booth_id': exhibitor.booth_id,
                    'booth_name': _localize_i18n_value(exhibitor.booth_name, locale),
                },
                status=status.HTTP_200_OK
            )
        except ExhibitorInfo.DoesNotExist:
            return Response(
                {'success': False, 'error': 'Invalid credentials'},
                status=status.HTTP_401_UNAUTHORIZED
            )


class ExhibitorInfoSerializer(I18nAwareModelSerializer):
    class Meta:
        model = ExhibitorInfo
        fields = (
            'id',
            'name',
            'description',
            'url',
            'email',
            'logo',
            'key',
            'lead_scanning_enabled',
        )


class ExhibitorInfoViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ExhibitorInfoSerializer
    lookup_field = 'id'

    def get_queryset(self):
        return ExhibitorInfo.objects.filter(event=self.request.event)


class LeadCreateView(views.APIView):
    def get_allowed_attendee_data(self, order_position, settings, exhibitor):
        allowed_fields = settings.all_allowed_fields
        attendee_data = {
            'name': order_position.attendee_name,
            'email': order_position.attendee_email,
            'company': order_position.company,
            'city': order_position.city if 'attendee_city' in allowed_fields else None,
            'country': str(order_position.country) if 'attendee_country' in allowed_fields else None,
            'note': '',
            'tags': []
        }

        return {k: v for k, v in attendee_data.items() if v is not None}

    def post(self, request, *args, **kwargs):
        pseudonymization_id = request.data.get('lead')
        scanned = request.data.get('scanned')
        scan_type = request.data.get('scan_type')
        device_name = request.data.get('device_name')
        open_event = request.data.get('open_event')
        key = request.headers.get('Exhibitor')

        # ---------------- VALIDATION ----------------
        missing_fields = []

        if not pseudonymization_id:
            missing_fields.append('lead')
        if scanned is None:
            missing_fields.append('scanned')
        if not scan_type:
            missing_fields.append('scan_type')
        if not device_name:
            missing_fields.append('device_name')

        if missing_fields:
            return Response(
                {
                    'success': False,
                    'error': f"Missing fields: {', '.join(missing_fields)}"
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        ALLOWED_SCAN_TYPES = ['qr', 'barcode', 'manual']
        if scan_type not in ALLOWED_SCAN_TYPES:
            return Response(
                {
                    'success': False,
                    'error': f"Invalid scan_type. Allowed values: {', '.join(ALLOWED_SCAN_TYPES)}"
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        # ---------------- AUTHENTICATION ----------------
        try:
            exhibitor = ExhibitorInfo.objects.get(key=key)
            settings_obj = ExhibitorSettings.objects.filter(event=exhibitor.event).first()

            if not settings_obj:
                return Response(
                    {
                        'success': False,
                        'error': 'Exhibitor settings not found'
                    },
                    status=status.HTTP_404_NOT_FOUND
                )

        except ExhibitorInfo.DoesNotExist:
            return Response(
                {'success': False, 'error': 'Invalid exhibitor key'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # ---------------- GET ATTENDEE ----------------
        try:
            if open_event:
                order_position = OrderPosition.objects.get(secret=pseudonymization_id)
            else:
                order_position = OrderPosition.objects.get(
                    pseudonymization_id=pseudonymization_id
                )
        except OrderPosition.DoesNotExist:
            return Response(
                {'success': False, 'error': 'Attendee not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        # ---------------- DUPLICATE CHECK ----------------
        existing_lead = Lead.objects.filter(
            exhibitor=exhibitor,
            pseudonymization_id=pseudonymization_id
        ).first()

        if existing_lead:
            attendee_data = self.get_allowed_attendee_data(
                order_position,
                settings_obj,
                exhibitor
            )
            return Response(
                {
                    'success': False,
                    'error': 'Lead already scanned',
                    'attendee': attendee_data
                },
                status=status.HTTP_409_CONFLICT
            )

        # ---------------- CREATE LEAD ----------------
        attendee_data = self.get_allowed_attendee_data(
            order_position,
            settings_obj,
            exhibitor
        )

        locale = _get_exhibitor_locale(exhibitor)

        lead = Lead.objects.create(
            exhibitor=exhibitor,
            exhibitor_name=_localize_i18n_value(exhibitor.name, locale),
            pseudonymization_id=pseudonymization_id,
            scanned=timezone.now(),
            scan_type=scan_type,
            device_name=device_name,
            booth_id=exhibitor.booth_id,
            booth_name=_localize_i18n_value(exhibitor.booth_name, locale),
            attendee=attendee_data
        )

        return Response(
            {
                'success': True,
                'lead_id': lead.id,
                'attendee': attendee_data
            },
            status=status.HTTP_201_CREATED
        )


class LeadRetrieveView(views.APIView):
    def get(self, request, *args, **kwargs):
        key = request.headers.get('Exhibitor')
        try:
            exhibitor = ExhibitorInfo.objects.get(key=key)
        except ExhibitorInfo.DoesNotExist:
            return Response(
                {'success': False, 'error': 'Invalid exhibitor key'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        leads = Lead.objects.filter(exhibitor=exhibitor).values(
            'id',
            'pseudonymization_id',
            'exhibitor_name',
            'scanned',
            'scan_type',
            'device_name',
            'booth_id',
            'booth_name',
            'attendee'
        )

        return Response(
            {
                'success': True,
                'leads': list(leads)
            },
            status=status.HTTP_200_OK
        )


class TagListView(views.APIView):
    def get(self, request, organizer, event, *args, **kwargs):
        key = request.headers.get('Exhibitor')
        try:
            exhibitor = ExhibitorInfo.objects.get(key=key)
            tags = ExhibitorTag.objects.filter(exhibitor=exhibitor)
            return Response({
                'success': True,
                'tags': [tag.name for tag in tags]
            })
        except ExhibitorInfo.DoesNotExist:
            return Response(
                {'success': False, 'error': 'Invalid exhibitor key'},
                status=status.HTTP_401_UNAUTHORIZED
            )


class LeadUpdateView(views.APIView):
    def post(self, request, organizer, event, lead_id, *args, **kwargs):
        key = request.headers.get('Exhibitor')
        note = request.data.get('note')
        tags = request.data.get('tags', [])

        try:
            exhibitor = ExhibitorInfo.objects.get(key=key)
        except ExhibitorInfo.DoesNotExist:
            return Response(
                {'success': False, 'error': 'Invalid exhibitor key'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        try:
            lead = Lead.objects.get(pseudonymization_id=lead_id, exhibitor=exhibitor)
        except Lead.DoesNotExist:
            return Response(
                {'success': False, 'error': 'Lead not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        attendee_data = lead.attendee or {}

        if note is not None:
            attendee_data['note'] = note
        if tags is not None:
            attendee_data['tags'] = tags

            for tag_name in tags:
                tag, created = ExhibitorTag.objects.get_or_create(
                    exhibitor=exhibitor,
                    name=tag_name
                )
                if not created:
                    tag.use_count += 1
                    tag.save()

        lead.attendee = attendee_data
        lead.save()

        return Response(
            {
                'success': True,
                'lead_id': lead.id,
                'attendee': lead.attendee
            },
            status=status.HTTP_200_OK
        )
