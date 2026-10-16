from django.http import JsonResponse, FileResponse, HttpResponse
from config.paths import PROJECT_ROOT

def live(request):
    return JsonResponse({"status": "alive"})

def ready(request):
    from django.db import connections
    from runtime.models import RuntimeState
    from library.models import LibrarySettings
    from config.paths import generation_root
    try:
        state = RuntimeState.objects.get(pk=1)
        LibrarySettings.objects.get(pk=1)
        matched = str(connections["library"].settings_dict["NAME"]) == str(generation_root(state.active_generation) / "library.sqlite3")
        return JsonResponse({"ready": matched}, status=200 if matched else 503)
    except Exception:
        return JsonResponse({"ready": False}, status=503)

def frontend(request):
    path = PROJECT_ROOT / "frontend" / "dist" / "index.html"
    if not path.exists():
        return HttpResponse("Frontend has not been built. Run npm run build in frontend/.", status=503, content_type="text/plain; charset=utf-8")
    return FileResponse(path.open("rb"), content_type="text/html; charset=utf-8")

def api_dispatch(request, route=""):
    from .api import dispatch
    return dispatch(request, route.strip("/"))
