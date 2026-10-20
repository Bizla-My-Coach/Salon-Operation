from django.urls import path
from django.contrib.auth import views as auth_views
from . import views
from . import workbook_views
from . import staff_import_views
from . import import_views
from . import review_views
from . import google_review_api
from . import google_review_views
urlpatterns=[
path('api/v1/google-reviews/ingest/', google_review_api.reviews, name='google_review_ingest'),
path('api/v1/google-reviews/publications/', google_review_api.publications, name='google_review_publications'),
path('reviews/google/', google_review_views.inbox, name='google_review_inbox'),
path('reviews/google/analytics/', google_review_views.analytics, name='google_review_analytics'),
path('reviews/google/assignments/', google_review_views.assignment_queue, name='google_review_assignments'),
path('reviews/google/<uuid:review_id>/', google_review_views.detail, name='google_review_detail'),
path('reviews/google/<uuid:review_id>/action/', google_review_views.action, name='google_review_action'),
path('reviews/', review_views.inbox, name='review_inbox'),
path('reviews/new/', review_views.create, name='review_create'),
path('reviews/reports/', review_views.reports, name='review_reports'),
path('reviews/notifications/', review_views.notifications, name='review_notifications'),
path('reviews/notifications/read-all/', review_views.notifications_read_all, name='review_notifications_read_all'),
path('reviews/notifications/<int:notification_id>/open/', review_views.notification_open, name='review_notification_open'),
path('reviews/<uuid:case_id>/', review_views.detail, name='review_detail'),
path('reviews/<uuid:case_id>/action/', review_views.action, name='review_action'),
path('imports/', import_views.import_data, name='import_data'),
path('imports/branches/', import_views.branch_csv_import, name='branch_csv_import'),
path('imports/branches/template/', import_views.branch_csv_template, name='branch_csv_template'),
path('admin-panel/import/workbook/', workbook_views.workbook_import, name='workbook_import'),
path('admin-panel/import/workbook/<uuid:import_id>/snapshot/', workbook_views.workbook_snapshot, name='workbook_snapshot'),
path('visits/<int:visit_id>/', views.visit_detail, name='visit_detail'),
path('profile/', views.my_profile, name='my_profile'),
path('profile/change-password/', views.change_password, name='change_password'),
path('manager/staff/', views.manager_staff, name='manager_staff'),
path('general-manager/staff/', views.general_manager_staff, name='general_manager_staff'),
path('admin-panel/staff/', views.general_manager_staff, name='admin_staff'),
path('staff/import/', staff_import_views.staff_csv_import, name='staff_csv_import'),
path('staff/import/template/', staff_import_views.staff_csv_template, name='staff_csv_template'),
path('staff/<int:user_id>/reset-password/', views.staff_password_reset, name='staff_password_reset'),
path('health/',views.health,name='health'),path('login/',auth_views.LoginView.as_view(template_name='registration/login.html',redirect_authenticated_user=True,extra_context={'plain_layout':True}),name='login'),path('logout/',views.logout_view,name='logout'),path('',views.dashboard,name='dashboard'),
path('admin-panel/',views.admin_dashboard,name='admin_dashboard'),path('admin-panel/reports/',views.admin_reports,name='admin_reports'),path('admin-panel/visits/',views.admin_visits,name='admin_visits'),path('admin-panel/users/add/',views.create_user,name='create_user'),path('admin-panel/roster/',views.branch_roster,name='branch_roster'),path('admin-panel/import/',views.csv_import,name='csv_import'),path('admin-panel/services/',views.service_catalog,name='service_catalog'),path('general-manager/',views.general_manager_dashboard,name='general_manager_dashboard'),
path('manager/',views.manager_dashboard,name='manager_dashboard'),path('manager/visit/new/',views.new_visit,name='new_visit'),path('manager/visit/<int:visit_id>/edit-services/',views.edit_visit_services,name='edit_visit_services'),path('manager/service/<int:pk>/verify/',views.verify_service,name='verify_service'),path('manager/visit/<int:visit_id>/verify/',views.verify_visit,name='verify_visit'),path('manager/service/<int:pk>/cancel-reassign/',views.cancel_and_reassign_service,name='cancel_and_reassign_service'),path('manager/visit/<int:visit_id>/invoice/',views.add_invoice,name='add_invoice'),path('manager/visit/<int:visit_id>/feedback/',views.collect_feedback,name='collect_feedback'),
path('employee/',views.employee_dashboard,name='employee_dashboard'),path('employee/service/<int:pk>/',views.execute_service,name='execute_service'),path('employee/task/<int:pk>/<str:action>/',views.task_action,name='task_action'),path('employee/service/<int:pk>/finish/',views.finish_service,name='finish_service'),
path('feedback/<uuid:token>/',views.feedback_form,name='feedback_form')]
