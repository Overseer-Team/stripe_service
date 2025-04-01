# stripe_service

This repository contains the code for a simple Litestar microservice that handles patron tracking via Stripe.

It contains two main endpoints:
- `/shop/checkout` `-` A wrapper for Stripe's checkout session creation
- `/shop/webhook` `-` A webhook endpoint to listen for successful Stripe checkouts
  - Adds record to "patron" table with asyncpg
  - Redirects to success page

## Authentication

All requests must be authorised with unique secret tokens on above endpoints.
