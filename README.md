# TechBloom Laptops - Event-Driven Serverless Order Processing

An event-driven serverless order-processing system built on Microsoft Azure. Customers can select a laptop, submit an order, and receive an automated confirmation or rejection email.

## Live Website

[Open the TechBloom Laptops website](https://blue-flower-010269b0f.7.azurestaticapps.net)

## Project Overview

The frontend website displays ten laptop products and allows customers to submit orders. The backend processes each order using five Python Azure Functions.

The system uses Azure Storage Queues so the Functions can process different tasks independently and asynchronously.

## Order Processing Flow

```text
Customer
   ↓
submit_order
   ↓
orders-incoming queue
   ↓
validate_order
   ├── Valid order → orders-to-email → send_confirmation_email
   ├── Valid order → orders-to-log → log_to_table
   └── Invalid order → orders-invalid → send_rejection_email
   ```
## Five Azure Functions

| Function | Trigger | Purpose |
|---|---|---|
| `submit_order` | HTTP trigger | Receives an order from the website and places it in the incoming queue |
| `validate_order` | Queue trigger | Validates the order, checks inventory, and reduces the available stock |
| `send_confirmation_email` | Queue trigger | Sends a confirmation email for accepted orders |
| `send_rejection_email` | Queue trigger | Sends a rejection email for invalid or out-of-stock orders |
| `log_to_table` | Queue trigger | Saves accepted orders permanently in the Orders table |

## Azure Services Used

- Azure Static Web Apps
- Azure Functions with Python and Flex Consumption
- Azure Storage Queues
- Azure Table Storage
- Azure Communication Services Email
- Azure Key Vault
- User-assigned Managed Identity
- Application Insights
- GitHub Actions

## Storage Queues

The project uses four main queues:

- `orders-incoming`
- `orders-invalid`
- `orders-to-email`
- `orders-to-log`

## Table Storage

### LaptopInventory

Stores the laptop name, brand, price, SKU, and available stock.

### Orders

Stores accepted order records, including the customer, product, quantity, price, status, and order ID.

## Email Notifications

The system sends three types of emails:

1. Confirmation email for an accepted order
2. Rejection email for an invalid or out-of-stock order
3. Low-stock alert when the remaining stock reaches the configured threshold

## Security

A user-assigned managed identity allows the Function App to access Azure Storage queues and tables without storing passwords in the code.

Azure Key Vault securely stores the Azure Communication Services email connection string. The Function App accesses Key Vault and uses the connection string to send emails.

No passwords, access keys, or connection strings are stored in this GitHub repository.

## Monitoring

Application Insights is used to monitor:

- How many times each Function runs
- The average execution time of each Function
- Function performance and failures

## Project Files

| File | Purpose |
|---|---|
| `index.html` | Main laptop catalogue page |
| `order.html` | Customer order form |
| `function_app.py` | Python code for all five Azure Functions |
| `requirements.txt` | Required Python packages |
| `host.json` | Azure Functions runtime configuration |
| `.funcignore` | Excludes unnecessary files during deployment |
| `.github/workflows` | GitHub Actions workflow for the Static Web App |

## Deployment

The frontend files are stored in GitHub and automatically deployed to Azure Static Web Apps using GitHub Actions.

The Python backend was created in Visual Studio Code and deployed to Azure Functions.

## Author

**Ashdeep Singh Grewal**  
George Brown Polytechnic — Work Integrated Project
