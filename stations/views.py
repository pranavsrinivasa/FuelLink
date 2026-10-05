from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import RouteRequestSerializer
from .services.planner import get_cached_payload, plan_route
from .services.routing import RoutingError


class RouteView(APIView):

    def get(self, request):
        return self._handle(request, request.query_params)

    def post(self, request):
        return self._handle(request, request.data)

    def _handle(self, request, data):
        serializer = RouteRequestSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        try:
            payload = plan_route(serializer.validated_data['start'],
                                 serializer.validated_data['finish'])
        except RoutingError as exc:
            return Response({'error': exc.message}, status=exc.status_code)
        payload = dict(payload)
        payload['map_url'] = request.build_absolute_uri(
            reverse('route-map', args=[payload['route_id']]))
        return Response(payload, status=status.HTTP_200_OK)


def route_map(request, route_id):
    payload = get_cached_payload(route_id)
    if payload is None:
        raise Http404('This route is no longer cached. Request it again via /api/route/.')
    return render(request, 'route_map.html', {'route': payload})
