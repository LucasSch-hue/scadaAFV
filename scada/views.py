from django.shortcuts import render
from django.http import HttpResponse, Http404
from django.conf import settings
from io import StringIO
from .data_processor import process_scada_data

# Create your views here.


def dashboard(request):
    if request.method == 'POST':
        token = request.POST.get('token')
        start_date = request.POST.get('start_date', '2025-01-01 00:00:00')
        end_date = request.POST.get('end_date', '2025-12-31 23:59:59')
        targets = request.POST.get('targets', 'solar radiation,temperature,wind speed').split(',')

        try:
            df_plot, plot_htmls = process_scada_data(token, start_date, end_date, [t.strip() for t in targets])

            request.session['scada_download_params'] = {
                'token': token,
                'start_date': start_date,
                'end_date': end_date,
                'targets': [t.strip() for t in targets],
            }

            context = {
                'plots': plot_htmls,
                'csv_url': '/download_csv/',
            }
            return render(request, 'dashboard.html', context)
        except Exception as e:
            return render(request, 'dashboard.html', {'error': str(e)})
    else:
        return render(request, 'dashboard.html')

def download_csv(request):
    params = request.session.get('scada_download_params')
    if not params:
        raise Http404("No hay datos procesados para descargar. Procesa primero los datos.")

    df_plot, _ = process_scada_data(
        params['token'],
        params['start_date'],
        params['end_date'],
        params['targets']
    )

    buffer = StringIO()
    df_plot.to_csv(buffer, index=False, encoding='utf-8-sig')
    buffer.seek(0)

    response = HttpResponse(buffer.getvalue(), content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="datos_G_V_T_2025.csv"'
    return response
    