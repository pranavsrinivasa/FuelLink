from rest_framework import serializers


class LocationField(serializers.Field):

    default_error_messages = {
        'invalid': 'Provide a place name, "lat,lon", or an object with numeric lat and lon.',
        'range': 'lat must be within -90..90 and lon within -180..180.',
    }

    def to_internal_value(self, data):
        if isinstance(data, dict):
            try:
                lat, lon = float(data['lat']), float(data.get('lon', data.get('lng')))
            except (KeyError, TypeError, ValueError):
                self.fail('invalid')
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                self.fail('range')
            return {'lat': lat, 'lon': lon}
        if isinstance(data, str) and data.strip():
            return data.strip()
        self.fail('invalid')

    def to_representation(self, value):
        return value


class RouteRequestSerializer(serializers.Serializer):
    start = LocationField()
    finish = LocationField()
