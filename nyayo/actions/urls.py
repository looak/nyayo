from django.urls import path

from nyayo.lab import machines_views
from nyayo.lab import views as lab

from . import views

# Step 4 and step 6 each swap their stubs' lines for real views, wrapped in access.action_required.
urlpatterns = [
    path("sign-in", views.SignIn.as_view(), name="sign_in"),
    path("sign-out", views.SignOut.as_view(), name="sign_out"),
    path("create/test", lab.create_test_page, name="create_test"),
    path("create/test/lookup", lab.lookup, name="create_test_lookup"),
    path("create/test/branches", lab.branches, name="create_test_branches"),
    path("tests/<int:test_id>/<str:action>", lab.modify, name="test_action"),
    path("create/tune", views.stub("create_tune"), name="create_tune"),
    path("create/datagen", views.stub("create_datagen"), name="create_datagen"),
    path("upload-net", views.stub("upload_net"), name="upload_net"),
    path("gauntlet", views.stub("gauntlet"), name="gauntlet"),
    path("engines", views.stub("engines"), name="engines"),
    path("books", views.stub("books"), name="books"),
    path("machines", machines_views.machines_page, name="machines"),
    path("machine-place", machines_views.machine_place, name="machine_place"),
    path("machines/<path:name>", machines_views.machine_page, name="machine"),
]
