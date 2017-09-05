import json
from urllib import parse
from functools import lru_cache
from django.http import HttpResponse
from django.http import Http404
from django.template import loader
from django.utils.text import get_text_list
from .models import *
from django.db.models import Q, F, Sum
from django.views.generic.base import TemplateView
from django.views.generic.detail import DetailView
from django.urls import reverse
from django.shortcuts import render, redirect
from django.contrib import messages
from djgeojson.views import GeoJSONLayerView
from .models import LugarVotacion, Circuito
from fiscales.models import  Voluntario, VotoMesaOficial, VotoMesaReportado
from django.contrib.admin.views.decorators import staff_member_required
from django.utils.decorators import method_decorator
from django.contrib.auth.decorators import user_passes_test


class StaffOnlyMixing:

    @method_decorator(staff_member_required)
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)


class LugaresVotacionGeoJSON(GeoJSONLayerView):
    model = LugarVotacion
    properties = ('id', 'color')    # 'popup_html',)

    def get_queryset(self):
        qs = super().get_queryset()
        ids = self.request.GET.get('ids')
        if ids:
            qs = qs.filter(id__in=ids.split(','))
        elif 'todas' in self.request.GET:
            return qs
        elif 'testigo' in self.request.GET:
            qs = qs.filter(mesas__es_testigo=True).distinct()

        return qs


class ResultadosOficialesGeoJSON(GeoJSONLayerView):
    model = LugarVotacion
    properties = ('id', 'nombre', 'direccion_completa',
                  'seccion', 'circuito', 'resultados_oficiales')

    def get_queryset(self):
        qs = super().get_queryset()
        if 'seccion' in self.request.GET:
            return qs.filter(circuito__seccion__id__in=self.request.GET.getlist('seccion'))
        elif 'circuito' in self.request.GET:
            return qs.filter(circuito__id__in=self.request.GET.getlist('circuito'))
        elif 'lugarvotacion' in self.request.GET:
            return qs.filter(id__in=self.request.GET.getlist('lugarvotacion'))

        elif 'mesa' in self.request.GET:
            return qs.filter(mesas__id__in=self.request.GET.getlist('mesa')).distinct()

        return qs



class EscuelaDetailView(StaffOnlyMixing, DetailView):
    template_name = "elecciones/detalle_escuela.html"
    model = LugarVotacion


class ResultadoEscuelaDetailView(StaffOnlyMixing, DetailView):
    template_name = "elecciones/resultados_escuela.html"
    model = LugarVotacion


    def get_context_data(self, **kwargs):

        context = super().get_context_data(**kwargs)
        sum_por_partido = {}
        for nombre, id in Partido.objects.values_list('nombre', 'id'):
            sum_por_partido[nombre] = Sum(Case(When(opcion__partido__id=id, then=F('votos')),
                             output_field=IntegerField()))

        for nombre, id in Opcion.objects.filter(id__in=[16, 17, 18, 19]).values_list('nombre', 'id'):
            sum_por_partido[nombre] = Sum(Case(When(opcion__id=id, then=F('votos')),
                             output_field=IntegerField()))


        result = VotoMesaOficial.objects.filter(mesa__eleccion__id=1, mesa__lugar_votacion=self.object).aggregate(
            **sum_por_partido
        )
        total = sum(v for v in result.values() if v)
        result = {k: (v, f'{v*100/total:.2f}') for k, v in result.items() if v}
        context['resultados'] = result
        return context



# Create your views here.
class Mapa(StaffOnlyMixing, TemplateView):
    template_name = "elecciones/mapa.html"

    def get_context_data(self, **kwargs):

        context = super().get_context_data(**kwargs)

        geojson_url = reverse("geojson")
        if 'ids' in self.request.GET:
            query = self.request.GET.urlencode()
            geojson_url += f'?{query}'
        elif 'testigo' in self.request.GET:
            query = 'testigo=si'
            geojson_url += f'?{query}'

        context['geojson_url'] = geojson_url
        return context


class Resultados(StaffOnlyMixing, TemplateView):
    template_name = "elecciones/resultados.html"


    @classmethod
    @lru_cache(128)
    def agregaciones(cls):

        opciones = {}

        for id in Opcion.objects.values_list('id', flat=True):
            opciones[str(id)] = Sum(Case(When(opcion__id=id, then=F('votos')),
                                          output_field=IntegerField()))

        return opciones

    @property
    @lru_cache(128)
    def filtros(self):
        """a partir de los argumentos de urls, devuelve
        listas de seccion / circuito etc. para filtrar """
        if 'mesa' in self.request.GET:
            return Mesa.objects.filter(id__in=self.request.GET.getlist('mesa'))
        elif 'lugarvotacion' in self.request.GET:
            return LugarVotacion.objects.filter(id__in=self.request.GET.getlist('lugarvotacion'))
        elif 'circuito' in self.request.GET:
            return Circuito.objects.filter(id__in=self.request.GET.getlist('circuito'))
        elif 'seccion' in self.request.GET:
            return Seccion.objects.filter(id__in=self.request.GET.getlist('seccion'))


    @property
    @lru_cache(128)
    def electores(self):
        lookups = Q()
        meta = {}
        for eleccion in Eleccion.objects.all():

            if self.filtros:

                if 'mesa' in self.request.GET:
                    lookups = Q(mesas__id__in=self.filtros, mesas__eleccion=eleccion)

                elif 'lugarvotacion' in self.request.GET:
                    lookups = Q(id__in=self.filtros)

                elif 'circuito' in self.request.GET:
                    lookups = Q(circuito__in=self.filtros)

                elif 'seccion' in self.request.GET:
                    lookups = Q(circuito__seccion__in=self.filtros)

            escuelas = LugarVotacion.objects.filter(lookups).distinct()
            electores = escuelas.aggregate(v=Sum('electores'))['v']
            if electores and 'mesa' in self.request.GET:
                # promediamos los electores por mesa
                electores = electores * self.filtros.count() // Mesa.objects.filter(lugar_votacion__in=escuelas, eleccion=eleccion).count()
            meta[eleccion] = electores or 0
        return meta


    def get_resultados(self):
        lookups = Q()
        resultados = {}
        agregaciones = Resultados.agregaciones()
        for eleccion in Eleccion.objects.all():

            if self.filtros:

                if 'mesa' in self.request.GET:
                    lookups = Q(mesa__in=self.filtros)

                elif 'lugarvotacion' in self.request.GET:
                    lookups = Q(mesa__lugar_votacion__in=self.filtros)

                elif 'circuito' in self.request.GET:
                    lookups = Q(mesa__circuito__in=self.filtros)

                elif 'seccion' in self.request.GET:
                    lookups = Q(mesa__circuito__seccion__in=self.filtros)


            electores = self.electores[eleccion]
            # primero para partidos
            result = VotoMesaReportado.objects.filter(
                Q(mesa__eleccion=eleccion) & lookups
            ).aggregate(
                **agregaciones
            )
            result = {Opcion.objects.get(id=k): v for k, v in result.items() if v is not None}

            positivos = sum(v for k, v in result.items() if k.es_partido)

            total = sum(result.values())
            result = {k: (v, f'{v*100/total:.2f}') for k, v in result.items()}
            resultados[eleccion] = {'tabla': result,
                                    'electores': electores,
                                    'positivos': positivos,
                                    'escrutados': total,
                                    'participacion': f'{total*100/electores:.2f}' if electores else '-'
                                }
        return resultados


    def menu_activo(self):
        if not self.filtros:
            return []
        elif isinstance(self.filtros[0], Seccion):
            return (self.filtros[0], None)
        elif isinstance(self.filtros[0], Circuito):
            return (self.filtros[0].seccion, self.filtros[0])


    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.filtros:
            context['para'] = get_text_list(list(self.filtros), " y ")
        else:
            context['para'] = 'Buenos Aires'

        context['secciones'] = Seccion.objects.all()
        context['resultados'] = self.get_resultados()
        context['menu_activo'] = self.menu_activo()
        return context

