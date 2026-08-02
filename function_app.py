import azure.functions as func
import json
import uuid
import logging
import os
import re
from datetime import datetime, timezone

from azure.communication.email import EmailClient
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableClient, UpdateMode
from azure.identity import ManagedIdentityCredential

app = func.FunctionApp()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

PRICE_CATALOG = {
    "ASUS-ZB14": 1499.99,
    "ACER-SG14": 1249.99,
    "DELL-XPS13": 1699.99,
    "LEN-YSL7": 1599.99,
    "HP-OB5-16": 1199.99,
    "MS-SL8": 2299.99,
    "SAM-GB5P14": 1749.99,
    "LG-GPRO17": 2499.99,
    "MSI-P16AI": 1899.99,
    "ASUS-G14": 2199.99,
}

PRODUCT_NAMES = {
    "ASUS-ZB14": "ASUS Zenbook 14 OLED",
    "ACER-SG14": "Acer Swift Go 14 AI",
    "DELL-XPS13": "Dell XPS 13",
    "LEN-YSL7": "Lenovo Yoga Slim 7i Aura Edition",
    "HP-OB5-16": "HP OmniBook 5 16",
    "MS-SL8": "Microsoft Surface Laptop 13.8-inch (8th Edition)",
    "SAM-GB5P14": "Samsung Galaxy Book5 Pro 14",
    "LG-GPRO17": "LG gram Pro 17",
    "MSI-P16AI": "MSI Prestige 16 AI Evo",
    "ASUS-G14": "ASUS ROG Zephyrus G14",
}


def get_table_client(table_name: str) -> TableClient:
    local_connection = os.getenv("ProjectStorage")

    # Local testing with Azurite
    if local_connection:
        return TableClient.from_connection_string(
            conn_str=local_connection,
            table_name=table_name
        )

    # Deployed Azure Function using the user-assigned managed identity
    table_service_uri = os.getenv("ProjectStorage__tableServiceUri")
    client_id = os.getenv("ProjectStorage__clientId")

    if not table_service_uri or not client_id:
        raise RuntimeError("ProjectStorage table settings are missing.")

    credential = ManagedIdentityCredential(client_id=client_id)

    return TableClient(
        endpoint=table_service_uri,
        table_name=table_name,
        credential=credential
    )

# Function 1: receives an order and places it in orders-incoming
@app.route(
    route="submit_order",
    methods=["POST"],
    auth_level=func.AuthLevel.ANONYMOUS
)
@app.queue_output(
    arg_name="queue_out",
    queue_name="orders-incoming",
    connection="ProjectStorage"
)
def submit_order(
    req: func.HttpRequest,
    queue_out: func.Out[str]
) -> func.HttpResponse:

    try:
        order = req.get_json()
    except ValueError:
        return func.HttpResponse(
            json.dumps({
                "status": "error",
                "message": "Request body must be valid JSON."
            }),
            status_code=400,
            mimetype="application/json"
        )

    if not isinstance(order, dict):
        return func.HttpResponse(
            json.dumps({
                "status": "error",
                "message": "Order must be a JSON object."
            }),
            status_code=400,
            mimetype="application/json"
        )

    required_fields = ["name", "email", "product", "quantity"]

    missing_fields = [
        field
        for field in required_fields
        if order.get(field) in (None, "")
    ]

    if missing_fields:
        return func.HttpResponse(
            json.dumps({
                "status": "rejected",
                "message": "Required information is missing.",
                "missing_fields": missing_fields
            }),
            status_code=400,
            mimetype="application/json"
        )

    order["order_id"] = str(uuid.uuid4())
    order["submitted_at"] = datetime.now(timezone.utc).isoformat()
    order["status"] = "RECEIVED"

    queue_out.set(json.dumps(order))

    return func.HttpResponse(
        json.dumps({
            "status": "received",
            "message": "Order received and placed in the queue.",
            "order_id": order["order_id"]
        }),
        status_code=202,
        mimetype="application/json"
    )

# Function 2: validates the order and routes it to the correct queues
@app.queue_trigger(
    arg_name="msg",
    queue_name="orders-incoming",
    connection="ProjectStorage"
)
@app.queue_output(
    arg_name="email_out",
    queue_name="orders-to-email",
    connection="ProjectStorage"
)
@app.queue_output(
    arg_name="log_out",
    queue_name="orders-to-log",
    connection="ProjectStorage"
)
@app.queue_output(
    arg_name="invalid_out",
    queue_name="orders-invalid",
    connection="ProjectStorage"
)
def validate_order(
    msg: func.QueueMessage,
    email_out: func.Out[str],
    log_out: func.Out[str],
    invalid_out: func.Out[str]
) -> None:

    order = json.loads(msg.get_body().decode("utf-8"))

    logging.info(
        "Validating order %s",
        order.get("order_id")
    )

    errors = []

    email = str(order.get("email", "")).strip()

    if not EMAIL_RE.match(email):
        errors.append("invalid email format")

    try:
        quantity = int(order.get("quantity", 0))

        if quantity < 1 or quantity > 50:
            errors.append("quantity must be between 1 and 50")
    except (ValueError, TypeError):
        quantity = 0
        errors.append("quantity must be a number")
    product = str(order.get("product", "")).strip()

    if product not in PRICE_CATALOG:
        errors.append("product not found in catalog")

    # Check inventory only after the basic information is valid
    if not errors:
        inventory_table = get_table_client("LaptopInventory")

        try:
            laptop = inventory_table.get_entity(
                partition_key="LAPTOP",
                row_key=product
            )

            stock = int(laptop.get("Stock", 0))

            if stock < quantity:
                errors.append(
                    f"out of stock (only {stock} available)"
                )
            else:
                new_stock = stock - quantity
                laptop["Stock"] = new_stock

                inventory_table.update_entity(
                    entity=laptop,
                    mode=UpdateMode.REPLACE
                )
                # Send an admin warning when remaining stock is low
                LOW_STOCK_THRESHOLD = 5

                if new_stock <= LOW_STOCK_THRESHOLD:
                    try:
                        admin_email = os.getenv("ADMIN_EMAIL")
                        connection_string = os.getenv(
                            "ACS_CONNECTION_STRING"
                        )
                        sender_address = os.getenv(
                            "ACS_SENDER_ADDRESS"
                        )
                        product_name = PRODUCT_NAMES.get(
                            product,
                            product
                        )

                        if not admin_email:
                            raise RuntimeError(
                                "ADMIN_EMAIL setting is missing."
                            )

                        if not connection_string or not sender_address:
                            raise RuntimeError(
                                "Email settings are missing."
                            )

                        email_client = (
                            EmailClient.from_connection_string(
                                connection_string
                            )
                        )

                        warning_message = {
                            "senderAddress": sender_address,
                            "recipients": {
                                "to": [
                                    {
                                        "address": admin_email,
                                        "displayName": "TechBloom Admin"
                                    }
                                ]
                            },
                            "content": {
                                "subject": (
                                    f"Low Stock Warning: {product_name}"
                                ),
                                "plainText": (
                                    f"Low stock warning.\n\n"
                                    f"Laptop: {product_name}\n"
                                    f"SKU: {product}\n"
                                    f"Remaining stock: {new_stock}\n"
                                    f"Threshold: {LOW_STOCK_THRESHOLD}"
                                )
                            }
                        }

                        poller = email_client.begin_send(
                            warning_message
                        )
                        result = poller.result()

                        logging.info(
                            "Low-stock warning sent for %s. Status: %s",
                            product,
                            result.get("status")
                        )

                    except Exception:
                        logging.exception(
                            "Low-stock warning could not be sent for %s.",
                            product
                        )
                order["unit_price"] = PRICE_CATALOG[product]
                order["quantity"] = quantity

        except ResourceNotFoundError:
            errors.append("product inventory record not found")

    if errors:
        order["status"] = "REJECTED"
        order["validation_errors"] = errors

        invalid_out.set(json.dumps(order))

        logging.warning(
            "Order %s rejected: %s",
            order.get("order_id"),
            errors
        )

        return

    order["status"] = "VALID"
    order["validated_at"] = datetime.now(timezone.utc).isoformat()

    payload = json.dumps(order)

    email_out.set(payload)
    log_out.set(payload)

    logging.info(
        "Order %s validated successfully",
        order.get("order_id")
    )

    # Function 3: sends a confirmation email for a valid order
@app.queue_trigger(
    arg_name="msg",
    queue_name="orders-to-email",
    connection="ProjectStorage"
)
def send_confirmation_email(msg: func.QueueMessage) -> None:

    order = json.loads(msg.get_body().decode("utf-8"))

    connection_string = os.getenv("ACS_CONNECTION_STRING")
    sender_address = os.getenv("ACS_SENDER_ADDRESS")

    if not connection_string or not sender_address:
        raise RuntimeError("Azure Communication Services email settings are missing.")

    email_client = EmailClient.from_connection_string(connection_string)

    quantity = int(order.get("quantity", 0))
    unit_price = float(order.get("unit_price", 0))
    total_price = round(quantity * unit_price, 2)
    short_id = order.get("order_id", "")[:8].upper()
    product_sku = order.get("product", "Unknown")
    product_name = PRODUCT_NAMES.get(product_sku, "Unknown laptop")

    message = {
        "senderAddress": sender_address,
        "recipients": {
            "to": [
                {
                    "address": order["email"],
                    "displayName": order["name"]
                }
            ]
        },
        "content": {
            "subject": f"Order {short_id} Confirmed",
            "plainText": (
                f"Hello {order['name']},\n\n"
                f"Your order has been confirmed.\n\n"
                f"Order ID: {order['order_id']}\n"
                f"Laptop: {product_name}\n"
                f"SKU: {product_sku}\n"
                f"Quantity: {quantity}\n"
                f"Unit price: ${unit_price:.2f}\n"
                f"Total price: ${total_price:.2f}\n\n"
                f"Thank you for your order."
            ),
       "html": (
    f"<html><body style='margin:0;background:#f4f6fa;font-family:Arial,sans-serif;color:#111827;'>"
    f"<div style='max-width:620px;margin:0 auto;padding:24px;'>"

    f"<div style='background:#101626;padding:24px;border-radius:14px 14px 0 0;'>"
    f"<div style='font-size:24px;font-weight:bold;color:#ffffff;'>"
    f"Tech<span style='color:#17ae1f;'>Bloom</span> Laptops"
    f"</div>"
    f"</div>"

    f"<div style='background:#ffffff;padding:28px;border:1px solid #e2e8f0;"
    f"border-top:0;border-radius:0 0 14px 14px;'>"

    f"<div style='font-size:38px;text-align:center;'>✅</div>"
    f"<h2 style='text-align:center;margin:10px 0 8px;'>Order Confirmed</h2>"
    f"<p style='text-align:center;color:#64748b;'>"
    f"Hello {order['name']}, your order has been confirmed."
    f"</p>"

    f"<div style='background:#f0fdf4;border:1px solid #bbf7d0;"
    f"border-radius:10px;padding:16px;text-align:center;margin:22px 0;'>"
    f"<div style='font-size:12px;color:#64748b;'>ORDER NUMBER</div>"
    f"<div style='font-size:24px;font-weight:bold;color:#17ae1f;'>{short_id}</div>"
    f"</div>"

    f"<table style='width:100%;border-collapse:collapse;font-size:14px;'>"
    f"<tr><td style='padding:10px 0;color:#64748b;'>Laptop</td>"
    f"<td style='padding:10px 0;text-align:right;font-weight:bold;'>{product_name}</td></tr>"

    f"<tr><td style='padding:10px 0;color:#64748b;'>SKU</td>"
    f"<td style='padding:10px 0;text-align:right;'>{product_sku}</td></tr>"

    f"<tr><td style='padding:10px 0;color:#64748b;'>Quantity</td>"
    f"<td style='padding:10px 0;text-align:right;'>{quantity}</td></tr>"

    f"<tr><td style='padding:10px 0;color:#64748b;'>Unit price</td>"
    f"<td style='padding:10px 0;text-align:right;'>CAD ${unit_price:.2f}</td></tr>"

    f"<tr><td style='padding:12px 0;border-top:1px solid #e2e8f0;font-weight:bold;'>Total</td>"
    f"<td style='padding:12px 0;border-top:1px solid #e2e8f0;"
    f"text-align:right;font-size:19px;font-weight:bold;color:#17ae1f;'>"
    f"CAD ${total_price:.2f}</td></tr>"
    f"</table>"

    f"<p style='margin-top:24px;text-align:center;color:#64748b;'>"
    f"Thank you for shopping with TechBloom Laptops."
    f"</p>"

    f"<div style='margin-top:24px;padding-top:16px;border-top:1px solid #e2e8f0;"
    f"text-align:center;font-size:12px;color:#64748b;'>"
    f"Powered by Azure Functions · Azure Storage Queues · Azure Communication Services"
    f"</div>"

    f"</div></div></body></html>"
)
        }
    }

    poller = email_client.begin_send(message)
    result = poller.result()

    logging.info(
        "Confirmation email sent for order %s. Status: %s",
        order.get("order_id"),
        result.get("status")
    )


# Function 4: sends a rejection email for an invalid order
@app.queue_trigger(
    arg_name="msg",
    queue_name="orders-invalid",
    connection="ProjectStorage"
)
def send_rejection_email(msg: func.QueueMessage) -> None:

    order = json.loads(msg.get_body().decode("utf-8"))

    customer_email = str(order.get("email", "")).strip()

    # An email cannot be sent if the address itself is invalid
    if not EMAIL_RE.match(customer_email):
        logging.warning(
            "Rejection email skipped because order %s has an invalid email.",
            order.get("order_id")
        )
        return

    connection_string = os.getenv("ACS_CONNECTION_STRING")
    sender_address = os.getenv("ACS_SENDER_ADDRESS")

    if not connection_string or not sender_address:
        raise RuntimeError("Azure Communication Services email settings are missing.")

    email_client = EmailClient.from_connection_string(connection_string)

    reasons = order.get(
        "validation_errors",
        ["The order could not be processed."]
    )

    reason_text = "; ".join(reasons)
    customer_name = order.get("name", "Customer")
    short_id = order.get("order_id", "")[:8].upper()
    product_sku = str(order.get("product", "")).strip()
    product_name = PRODUCT_NAMES.get(
    product_sku,
    product_sku or "Unknown laptop"
)

    message = {
        "senderAddress": sender_address,
        "recipients": {
            "to": [
                {
                    "address": customer_email,
                    "displayName": customer_name
                }
            ]
        },
        "content": {
            "subject": f"Order {short_id} Could Not Be Processed",
            "plainText": (
                
                f"Hello {customer_name},\n\n"
                f"Your order could not be processed.\n\n"
                f"Order ID: {order.get('order_id', '')}\n"
                f"Laptop: {product_name}\n"
                f"SKU: {product_sku or 'Unknown'}\n"
                f"Reason: {reason_text}\n\n"
                f"Please check the order information and try again."
),
            "html": (
                f"<html><body style='margin:0;background:#f4f6fa;"
                f"font-family:Arial,sans-serif;color:#111827;'>"
                f"<div style='max-width:620px;margin:0 auto;padding:24px;'>"

                f"<div style='background:#101626;padding:24px;"
                f"border-radius:14px 14px 0 0;'>"
                f"<div style='font-size:24px;font-weight:bold;color:#ffffff;'>"
                f"Tech<span style='color:#17ae1f;'>Bloom</span> Laptops"
                f"</div>"
                f"</div>"

                f"<div style='background:#ffffff;padding:28px;"
                f"border:1px solid #e2e8f0;border-top:0;"
                f"border-radius:0 0 14px 14px;'>"

                f"<div style='font-size:38px;text-align:center;'>⚠️</div>"
                f"<h2 style='text-align:center;margin:10px 0 8px;'>"
                f"Order Could Not Be Processed"
                f"</h2>"

                f"<p style='text-align:center;color:#64748b;'>"
                f"Hello {customer_name}, there was a problem with your order."
                f"</p>"

                f"<div style='background:#fff7ed;border:1px solid #fed7aa;"
                f"border-radius:10px;padding:16px;text-align:center;"
                f"margin:22px 0;'>"
                f"<div style='font-size:12px;color:#64748b;'>ORDER NUMBER</div>"
                f"<div style='font-size:24px;font-weight:bold;color:#dc2626;'>"
                f"{short_id}</div>"
                f"</div>"

                f"<table style='width:100%;border-collapse:collapse;"
                f"font-size:14px;'>"

                f"<tr><td style='padding:10px 0;color:#64748b;'>Laptop</td>"
                f"<td style='padding:10px 0;text-align:right;"
                f"font-weight:bold;'>{product_name}</td></tr>"

                f"<tr><td style='padding:10px 0;color:#64748b;'>SKU</td>"
                f"<td style='padding:10px 0;text-align:right;'>"
                f"{product_sku or 'Unknown'}</td></tr>"
                f"</table>"

                f"<div style='background:#fef2f2;border:1px solid #fecaca;"
                f"border-radius:10px;padding:16px;margin-top:18px;"
                f"color:#b91c1c;'>"
                f"<strong>Reason:</strong> {reason_text}"
                f"</div>"

                f"<p style='margin-top:24px;text-align:center;"
                f"color:#64748b;'>"
                f"Please check the order information and try again."
                f"</p>"

                f"<div style='margin-top:24px;padding-top:16px;"
                f"border-top:1px solid #e2e8f0;text-align:center;"
                f"font-size:12px;color:#64748b;'>"
                f"Powered by Azure Functions · Azure Storage Queues · "
                f"Azure Communication Services"
                f"</div>"

                f"</div></div></body></html>"
            )
        }
    }

    poller = email_client.begin_send(message)
    result = poller.result()

    logging.info(
        "Rejection email sent for order %s. Status: %s",
        order.get("order_id"),
        result.get("status")
    )


# Function 5: saves a valid order permanently in the Orders table
@app.queue_trigger(
    arg_name="msg",
    queue_name="orders-to-log",
    connection="ProjectStorage"
)
def log_to_table(msg: func.QueueMessage) -> None:

    order = json.loads(msg.get_body().decode("utf-8"))

    quantity = int(order.get("quantity", 0))
    unit_price = float(order.get("unit_price", 0))

    entity = {
        "PartitionKey": order["submitted_at"][:10],
        "RowKey": order["order_id"],
        "CustomerName": order.get("name", ""),
        "CustomerEmail": order.get("email", ""),
        "Product": order.get("product", ""),
        "Quantity": quantity,
        "UnitPrice": unit_price,
        "TotalPrice": round(quantity * unit_price, 2),
        "SubmittedAt": order.get("submitted_at", ""),
        "ValidatedAt": order.get("validated_at", ""),
        "Status": order.get("status", "VALID")
    }

    orders_table = get_table_client("Orders")

    orders_table.upsert_entity(
        entity=entity,
        mode=UpdateMode.REPLACE
    )

    logging.info(
        "Order %s saved in the Orders table.",
        order.get("order_id")
    )