from django.contrib import admin
from .models import Payment



@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("merchant_invoice_number", "payment_id", "trx_id", "amount", "status", "updated_at")
    list_filter = ("status", "currency")
    search_fields = ("payment_id", "trx_id", "merchant_invoice_number")
    readonly_fields = ("created_at", "updated_at")
