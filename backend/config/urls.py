from django.urls import re_path
from .views import api_dispatch, frontend, live, ready

urlpatterns = [re_path(r"^health/live$", live), re_path(r"^health/ready$", ready), re_path(r"^api/v1(?:/(?P<route>.*))?$", api_dispatch), re_path(r"^(?:|login|library|classification|settings|transfers)$", frontend)]
