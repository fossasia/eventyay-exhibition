from django.conf import settings
from django.utils import timezone
from eventyay.api.serializers.i18n import I18nAwareModelSerializer
from eventyay.base.models import OrderPosition
from i18nfield.strings import LazyI18nString
from rest_framework import status, views, viewsets, serializers
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle

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
            'id', 'name', 'description', 'url',
            'email', 'logo', 'key', 'lead_scanning_enabled'
        )


class ExhibitorInfoViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ExhibitorInfoSerializer
    lookup_field = 'id'

    def get_queryset(self):
        return ExhibitorInfo.objects.filter(event=self.request.event)


# 🔥 Throttle
class LeadCreateThrottle(UserRateThrottle):
    scope = 'lead_create'


# 🔥 Serializer
class LeadCreateSerializer(serializers.Serializer):
    lead = serializers.CharField(required=True)
    scanned = serializers.CharField(required=True)
    scan_type = serializers.CharField(required=True)
    device_name = serializers.CharField(required=True)
class LeadCreateView(views.APIView):
    throttle_classes = [LeadCreateThrottle]
    throttle_scope = 'lead_create'

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
        # ✅ Validate request
        serializer = LeadCreateSerializer(data=request.data)

        if not serializer.is_valid():
            return Response(
                {'success': False, 'error': serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )

        # ✅ Extract data
        pseudonymization_id = serializer.validated_data['lead']
        scan_type = serializer.validated_data['scan_type']
        device_name = serializer.validated_data['device_name']

        open_event = request.data.get('open_event')
        key = request.headers.get('Exhibitor')

        # ✅ Authenticate
        try:
            exhibitor = ExhibitorInfo.objects.get(key=key)
            settings = ExhibitorSettings.objects.get(event=exhibitor.event)
        except (ExhibitorInfo.DoesNotExist, ExhibitorSettings.DoesNotExist):
            return Response(
                {'success': False, 'error': 'Invalid exhibitor key'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # ✅ Get attendee
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

        # ✅ Duplicate check
        if Lead.objects.filter(
            exhibitor=exhibitor,
            pseudonymization_id=pseudonymization_id
        ).exists():
            attendee_data = self.get_allowed_attendee_data(
                order_position, settings, exhibitor
            )

            return Response(
                {
                    'success': False,
                    'error': 'Lead already scanned',
                    'attendee': attendee_data
                },
                status=status.HTTP_409_CONFLICT
            )

        # ✅ Create lead
        attendee_data = self.get_allowed_attendee_data(
            order_position, settings, exhibitor
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

