import json
import uuid
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.db import transaction
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render

from .models import Payment


REQUEST_TIMEOUT_SECONDS = 20
PAYMENT_AMOUNT = Decimal("500.00")
PAYMENT_CURRENCY = "BDT"


def bkash_url(path):
    return f"{settings.BKASH_SANDBOX_URL.rstrip('/')}/{path.lstrip('/')}"


def response_data(response):
    """Return bKash JSON, including its occasionally malformed error JSON."""
    try:
        return response.json()
    except requests.exceptions.JSONDecodeError:
        try:
            return json.loads(response.text, strict=False)
        except json.JSONDecodeError:
            return {"statusMessage": response.text[:500] or "Empty response from bKash."}


def bkash_headers(token=None):
    headers = {"Accept": "application/json", "Content-Type": "application/json", "X-APP-Key": settings.BKASH_APP_KEY}
    if token:
        headers["Authorization"] = token
    return headers


def get_bkash_token():
    response = requests.post(
        bkash_url("tokenized/checkout/token/grant"),
        json={"app_key": settings.BKASH_APP_KEY, "app_secret": settings.BKASH_APP_SECRET},
        headers={"Accept": "application/json", "Content-Type": "application/json", "username": settings.BKASH_USERNAME, "password": settings.BKASH_PASSWORD},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    data = response_data(response)
    if response.ok and data.get("id_token"):
        return data["id_token"], None
    return None, data.get("statusMessage", f"Token request failed with HTTP {response.status_code}.")


def response_matches_payment(data, payment):
    """Reject a gateway response if it does not belong to the pending payment."""
    try:
        response_amount = Decimal(str(data.get("amount")))
    except (InvalidOperation, TypeError):
        return False
    return (
        data.get("paymentID") == payment.payment_id
        and data.get("merchantInvoiceNumber") == payment.merchant_invoice_number
        and response_amount == payment.amount
        and data.get("currency") == payment.currency
    )


def initiate_payment(request):
    """Create, save, then redirect to a bKash checkout session."""
    invoice = f"INV-{uuid.uuid4().hex[:16]}"
    try:
        token, error = get_bkash_token()
        if not token:
            return render(request, "payments/error.html", {"message": f"bKash authentication failed: {error}"})
        response = requests.post(
            bkash_url("tokenized/checkout/create"),
            json={
                "mode": "0011", "payerReference": "01770618575",
                "callbackURL": request.build_absolute_uri("/bkash/callback/"),
                "amount": str(PAYMENT_AMOUNT), "currency": PAYMENT_CURRENCY,
                "intent": "sale", "merchantInvoiceNumber": invoice,
            },
            headers=bkash_headers(token), timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        return render(request, "payments/error.html", {"message": f"Could not reach bKash: {exc}"})

    data = response_data(response)
    if response.ok and data.get("statusCode") == "0000" and data.get("bkashURL") and data.get("paymentID"):
        Payment.objects.create(
            payment_id=data["paymentID"], merchant_invoice_number=invoice,
            amount=PAYMENT_AMOUNT, currency=PAYMENT_CURRENCY,
        )
        return redirect(data["bkashURL"])
    return render(request, "payments/error.html", {"message": data.get("statusMessage", f"Could not create payment (HTTP {response.status_code}).")})


def bkash_callback(request):
    """Execute an approved payment once; duplicate callbacks are safe."""
    status, payment_id = request.GET.get("status"), request.GET.get("paymentID")
    payment = Payment.objects.filter(payment_id=payment_id).first() if payment_id else None
    if payment is None:
        return render(request, "payments/error.html", {"message": "Unknown payment ID; no local payment record exists."})
    if status != "success":
        payment.status = Payment.Status.CANCELLED if status == "cancel" else Payment.Status.FAILED
        payment.gateway_message = f"Callback status: {status or 'missing'}"
        payment.save(update_fields=["status", "gateway_message", "updated_at"])
        return render(request, "payments/error.html", {"message": f"Payment was not completed. bKash status: {status or 'missing'}."})

    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(payment_id=payment_id)
        if payment.status == Payment.Status.COMPLETED:
            return render(request, "payments/success.html", {"trx_id": payment.trx_id, "duplicate": True})
        if payment.status == Payment.Status.PROCESSING:
            return render(request, "payments/error.html", {"message": "This payment is already being processed. Please refresh shortly."})
        payment.status = Payment.Status.PROCESSING
        payment.save(update_fields=["status", "updated_at"])

    try:
        token, error = get_bkash_token()
        if not token:
            raise requests.RequestException(f"Could not authenticate to execute payment: {error}")
        response = requests.post(bkash_url("tokenized/checkout/execute"), json={"paymentID": payment_id}, headers=bkash_headers(token), timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        payment.status, payment.gateway_message = Payment.Status.PENDING, str(exc)
        payment.save(update_fields=["status", "gateway_message", "updated_at"])
        return render(request, "payments/error.html", {"message": f"Could not execute payment: {exc}"})

    data = response_data(response)
    if response.ok and data.get("statusCode") == "0000" and data.get("transactionStatus") == "Completed" and response_matches_payment(data, payment):
        payment.status, payment.trx_id, payment.gateway_message = Payment.Status.COMPLETED, data.get("trxID", ""), data.get("statusMessage", "")
        payment.save(update_fields=["status", "trx_id", "gateway_message", "updated_at"])
        return render(request, "payments/success.html", {"trx_id": payment.trx_id})

    payment.status, payment.gateway_message = Payment.Status.FAILED, data.get("statusMessage", f"Payment execution failed (HTTP {response.status_code}).")
    payment.save(update_fields=["status", "gateway_message", "updated_at"])
    return render(request, "payments/error.html", {"message": payment.gateway_message})


@staff_member_required
def reconcile_payment(request, payment_id):
    """Admin-only recovery check for a payment left pending after an uncertain response."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    payment = get_object_or_404(Payment, payment_id=payment_id)
    try:
        token, error = get_bkash_token()
        if not token:
            raise requests.RequestException(error)
        response = requests.post(bkash_url("tokenized/checkout/payment/status"), json={"paymentID": payment.payment_id}, headers=bkash_headers(token), timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        return render(request, "payments/error.html", {"message": f"Could not reconcile payment: {exc}"})

    data = response_data(response)
    if response.ok and data.get("transactionStatus") == "Completed" and response_matches_payment(data, payment):
        payment.status, payment.trx_id, payment.gateway_message = Payment.Status.COMPLETED, data.get("trxID", ""), "Reconciled from bKash payment status."
        payment.save(update_fields=["status", "trx_id", "gateway_message", "updated_at"])
    else:
        payment.gateway_message = data.get("statusMessage", f"Reconciliation returned HTTP {response.status_code}.")
        payment.save(update_fields=["gateway_message", "updated_at"])
    return render(request, "payments/reconciliation.html", {"payment": payment, "gateway_data": data})
