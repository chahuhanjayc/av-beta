from django.urls import path
from . import views

app_name = 'migration'

urlpatterns = [
    path('exit-control/', views.tally_exit_control, name='exit_control'),
    path('', views.import_sessions, name='sessions'),
    path('upload/', views.upload_migration, name='upload'),
    path('template/', views.download_import_template, name='template'),
    path('map/<int:pk>/', views.map_ledgers, name='map_ledgers'),
    path('preview/<int:pk>/', views.preview_migration, name='preview'),
    path('confirm/<int:pk>/', views.confirm_import, name='confirm'),
    path('approve/<int:pk>/', views.approve_import, name='approve_import'),
    path('approve/<int:pk>/revoke/', views.revoke_import_approval, name='revoke_import_approval'),
    path('summary/<int:pk>/', views.import_summary, name='summary'),
    path('cleanup/<int:pk>/export/', views.download_cleanup_issues, name='cleanup_export'),
    path('sync-risk/<int:pk>/export/', views.download_sync_risk, name='sync_risk_export'),
    path('cleanup/<int:pk>/tasks/', views.create_cleanup_tasks, name='cleanup_tasks'),
    path('reprocess/<int:pk>/', views.reprocess_row, name='reprocess_row'),
]
