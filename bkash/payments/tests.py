from decimal import Decimal
from unittest.mock import Mock, patch

from django.test import TestCase

from .models import Payment


def gateway_response(data, status_code=200):
    response = Mock()
    response.ok = status_code < 400
    response.status_code = status_code
    response.json.return_value = data
    return response


class PaymentFlowTests(TestCase):
    @patch("payments.views.requests.post")
    def test_create_saves_pending_payment_before_redirect(self, post):
        post.side_effect = [
            gateway_response({"id_token": "token"}),
            gateway_response({"statusCode": "0000", "paymentID": "PAY-1", "bkashURL": "https://sandbox.payment.bkash.com/example"}),
        ]

        response = self.client.get("/bkash/pay/")

        self.assertRedirects(response, "https://sandbox.payment.bkash.com/example", fetch_redirect_response=False)
        payment = Payment.objects.get(payment_id="PAY-1")
        self.assertEqual(payment.status, Payment.Status.PENDING)
        self.assertEqual(payment.amount, Decimal("500.00"))

    @patch("payments.views.requests.post")
    def test_completed_callback_is_not_executed_twice(self, post):
        payment = Payment.objects.create(
            payment_id="PAY-2", merchant_invoice_number="INV-2", amount=Decimal("500.00"), currency="BDT"
        )
        post.side_effect = [
            gateway_response({"id_token": "token"}),
            gateway_response({
                "statusCode": "0000", "transactionStatus": "Completed", "paymentID": "PAY-2",
                "merchantInvoiceNumber": "INV-2", "amount": "500.00", "currency": "BDT", "trxID": "TRX-2",
            }),
        ]

        first = self.client.get("/bkash/callback/?status=success&paymentID=PAY-2")
        second = self.client.get("/bkash/callback/?status=success&paymentID=PAY-2")

        payment.refresh_from_db()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(payment.status, Payment.Status.COMPLETED)
        self.assertEqual(payment.trx_id, "TRX-2")
        self.assertEqual(post.call_count, 2)  # grant + execute only for the first callback
