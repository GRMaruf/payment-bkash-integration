# payments/urls.py
from django.urls import path
from . import views

urlpatterns = [
    path('bkash/pay/', views.initiate_payment, name='bkash-pay'),
    path('bkash/callback/', views.bkash_callback, name='bkash-callback'),
    path('bkash/reconcile/<str:payment_id>/', views.reconcile_payment, name='bkash-reconcile'),
]
