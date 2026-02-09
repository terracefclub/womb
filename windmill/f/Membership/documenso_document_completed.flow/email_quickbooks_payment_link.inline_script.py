import email
import json
import smtplib
import typing
import wmill

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


Membership = typing.Literal["standard", "rca", "abroad", "sophomore"]
Installments = typing.Literal[1, 2]

class Member(typing.TypedDict):
    name: str
    email: str
    membership: Membership
    installments: Installments

def select_payment_link(membership: Membership, installments: Installments) -> str:
    return ""

def format_email(member: Member) -> tuple[str, str]:
    payment_link = select_payment_link(member["membership"], member["installments"])
    text = f"""
Hi {member["name"]},

Welcome to Terrace F Club! Thank you for completing your membership contract. We're excited to see you around the club.

Your membership details:
- Membership Type: {member["membership"]}
- Payment Schedule: {member["installments"]} installment{"s" if member["installments"] > 1 else ""}

Next Steps:
Please proceed to complete your dues payment at:
{payment_link}

If you have any questions, feel free to reply to this email.

Best regards,
The Terrace F Club Team
    """
    html = f"""
<!DOCTYPE html>
<html>
<head>
    <style>
        body {
            font-family: 'Helvetica Neue', Arial, sans-serif; 
            line-height: 1.6; 
            color: #1a1a1a;
            background-color: #f9f9f9;
            margin: 0;
            padding: 20px;
        }
        .email-wrapper {
            background-color: #ffffff;
            max-width: 600px;
            margin: 0 auto;
            border-radius: 8px;
            overflow: hidden;
        }
        .header {
            background-color: #2d5016;
            color: white;
            padding: 35px 30px;
            text-align: center;
            position: relative;
        }
        .header::after {
            content: '';
            position: absolute;
            bottom: 0;
            left: 0;
            right: 0;
            height: 3px;
            background: #c41e3a;
        }
        .header h1 {
            margin: 0;
            font-size: 26px;
            font-weight: 700;
            letter-spacing: 0.5px;
        }
        .content {
            padding: 35px 30px;
        }
        .content p {
            margin: 0 0 16px 0;
        }
        .greeting {
            font-size: 18px;
            color: #1a1a1a;
            margin-bottom: 18px;
            font-weight: 500;
        }
        .details-box {
            background-color: #f0f7ed;
            border-left: 4px solid #2d5016;
            padding: 20px;
            border-radius: 6px;
            margin: 25px 0;
        }
        .details-box strong {
            color: #2d5016;
            display: block;
            margin-bottom: 14px;
            font-size: 16px;
        }
        .detail-item {
            padding: 10px 0;
            border-bottom: 1px solid #d4e7cc;
        }
        .detail-item:last-child {
            border-bottom: none;
        }
        .detail-label {
            color: #4a4a4a;
            font-weight: 600;
            display: inline-block;
            min-width: 140px;
        }
        .detail-value {
            color: #1a1a1a;
        }
        .cta-section {
            text-align: center;
            margin: 30px 0;
            padding: 25px 0;
        }
        .cta-section p {
            font-size: 16px;
            font-weight: 600;
            color: #1a1a1a;
            margin-bottom: 18px;
        }
        .button {
            display: inline-block;
            padding: 14px 36px;
            background-color: #2d5016;
            color: white !important;
            text-decoration: none;
            border-radius: 6px;
            font-weight: 600;
            font-size: 16px;
            box-shadow: 0 3px 8px rgba(45, 80, 22, 0.25);
            transition: all 0.3s ease;
        }
        .button:hover {
            background-color: #3d6a1e;
            box-shadow: 0 5px 12px rgba(45, 80, 22, 0.35);
            transform: translateY(-1px);
        }
        .footer {
            background-color: #f9f9f9;
            padding: 25px 30px;
            text-align: center;
            color: #666;
            font-size: 14px;
            border-top: 2px solid #f0f7ed;
        }
        .footer p {
            margin: 6px 0;
        }
        .signature {
            color: #1a1a1a;
            font-weight: 600;
            margin-bottom: 15px;
        }
        .fun-note {
            font-size: 13px;
            color: #666;
            font-style: italic;
            margin-top: 15px;
        }
    </style>
</head>
<body>
    <div class="email-wrapper">
        <div class="header">
            <h1>Welcome to Terrace F Club</h1>
        </div>
        
        <div class="content">
            <p class="greeting">Hi {member["name"]},</p>
            
            <p>Thank you for completing your membership contract. We're excited to see you around the club!</p>
            
            <div class="details-box">
                <strong>Your Membership Details</strong>
                <div class="detail-item">
                    <span class="detail-label">Membership Type:</span>
                    <span class="detail-value">{member["membership"]}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">Payment Schedule:</span>
                    <span class="detail-value">{member["installments"]} installment{"s" if member["installments"] > 1 else ""}</span>
                </div>
            </div>
            
            <div class="cta-section">
                <p>Next Step: Complete Your Dues Payment</p>
                <a href="{payment_link}" class="button">Proceed to Payment</a>
            </div>
            
            <p>If you have any questions, feel free to reply to this email. We're here to help.</p>
        </div>
        
        <div class="footer">
            <p class="signature">Best regards,<br>The Terrace F Club Team</p>
            <p style="margin-top: 20px; color: #999; font-size: 12px;">
                This email was sent regarding your membership at Terrace F Club
            </p>
        </div>
    </div>
</body>
</html>
    """
    return text, html

def main(member: Member):
    msg = MIMEMultipart('alternative')
    msg["Subject"] = "Welcome to Terrace"
    msg["From"] = "business@terracefclub.org"
    msg["To"] = member["email"]

    text, html = format_email(member)
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP("smtp-relay.gmail.com", 587) as server:
        server.starttls()
        auth = json.loads(wmill.get_variable("u/tristanschrader/smtp"))
        server.login(auth["user"], auth["password"])
        server.send_message(msg)

# {
#     "event": "DOCUMENT_COMPLETED",
#     "payload": {
#         "event": "DOCUMENT_COMPLETED",
#         "payload": {
#             "id": 10,
#             "title": "documenso.pdf",
#             "source": "DOCUMENT",
#             "status": "COMPLETED",
#             "teamId": null,
#             "userId": 1,
#             "Recipient": [
#                 {
#                     "id": 50,
#                     "name": "Signer 2",
#                     "role": "VIEWER",
#                     "email": "signer2@documenso.com",
#                     "token": "SIGNING_TOKEN",
#                     "expired": null,
#                     "signedAt": "2026-01-28T22:44:02.948Z",
#                     "documentId": 10,
#                     "readStatus": "OPENED",
#                     "sendStatus": "SENT",
#                     "templateId": null,
#                     "authOptions": {
#                         "accessAuth": null,
#                         "actionAuth": null
#                     },
#                     "signingOrder": 1,
#                     "signingStatus": "SIGNED",
#                     "rejectionReason": null,
#                     "documentDeletedAt": null
#                 },
#                 {
#                     "id": 51,
#                     "name": "Signer 1",
#                     "role": "SIGNER",
#                     "email": "signer1@documenso.com",
#                     "token": "SIGNING_TOKEN",
#                     "expired": null,
#                     "signedAt": "2026-01-28T22:44:02.948Z",
#                     "documentId": 10,
#                     "readStatus": "OPENED",
#                     "sendStatus": "SENT",
#                     "templateId": null,
#                     "authOptions": {
#                         "accessAuth": null,
#                         "actionAuth": null
#                     },
#                     "signingOrder": 2,
#                     "signingStatus": "SIGNED",
#                     "rejectionReason": null,
#                     "documentDeletedAt": null
#                 }
#             ],
#             "createdAt": "2026-01-28T22:44:02.948Z",
#             "deletedAt": null,
#             "updatedAt": "2026-01-28T22:44:02.948Z",
#             "externalId": null,
#             "formValues": null,
#             "recipients": [
#                 {
#                     "id": 50,
#                     "name": "Signer 2",
#                     "role": "VIEWER",
#                     "email": "signer2@documenso.com",
#                     "token": "SIGNING_TOKEN",
#                     "expired": null,
#                     "signedAt": "2026-01-28T22:44:02.948Z",
#                     "documentId": 10,
#                     "readStatus": "OPENED",
#                     "sendStatus": "SENT",
#                     "templateId": null,
#                     "authOptions": {
#                         "accessAuth": null,
#                         "actionAuth": null
#                     },
#                     "signingOrder": 1,
#                     "signingStatus": "SIGNED",
#                     "rejectionReason": null,
#                     "documentDeletedAt": null
#                 },
#                 {
#                     "id": 51,
#                     "name": "Signer 1",
#                     "role": "SIGNER",
#                     "email": "signer1@documenso.com",
#                     "token": "SIGNING_TOKEN",
#                     "expired": null,
#                     "signedAt": "2026-01-28T22:44:02.948Z",
#                     "documentId": 10,
#                     "readStatus": "OPENED",
#                     "sendStatus": "SENT",
#                     "templateId": null,
#                     "authOptions": {
#                         "accessAuth": null,
#                         "actionAuth": null
#                     },
#                     "signingOrder": 2,
#                     "signingStatus": "SIGNED",
#                     "rejectionReason": null,
#                     "documentDeletedAt": null
#                 }
#             ],
#             "templateId": null,
#             "visibility": "EVERYONE",
#             "authOptions": null,
#             "completedAt": "2026-01-28T22:44:02.948Z",
#             "documentMeta": {
#                 "id": "doc_meta_123",
#                 "message": "Hello, please review and sign this document.",
#                 "subject": "Please sign this document",
#                 "language": "en",
#                 "password": null,
#                 "timezone": "UTC",
#                 "dateFormat": "MM/DD/YYYY",
#                 "redirectUrl": null,
#                 "signingOrder": "PARALLEL",
#                 "emailSettings": null,
#                 "distributionMethod": "EMAIL",
#                 "drawSignatureEnabled": true,
#                 "typedSignatureEnabled": true,
#                 "allowDictateNextSigner": false,
#                 "uploadSignatureEnabled": true
#             },
#             "documentDataId": "hs8qz1ktr9204jn7mg6c5dxy0"
#         },
#         "createdAt": "2026-01-28T22:44:02.948Z",
#         "webhookEndpoint": "https://app.windmill.dev/api/w/terrace-f-club/capture_u/http/flow/f.Membership.documenso_document_completed/f/Membership/documenso"
#     },
#     "createdAt": "2026-01-28T22:44:03.641Z",
#     "raw_string": "{\"event\":\"DOCUMENT_COMPLETED\",\"payload\":{\"createdAt\":\"2026-01-28T22:44:02.948Z\",\"event\":\"DOCUMENT_COMPLETED\",\"payload\":{\"Recipient\":[{\"authOptions\":{\"accessAuth\":null,\"actionAuth\":null},\"documentDeletedAt\":null,\"documentId\":10,\"email\":\"signer2@documenso.com\",\"expired\":null,\"id\":50,\"name\":\"Signer 2\",\"readStatus\":\"OPENED\",\"rejectionReason\":null,\"role\":\"VIEWER\",\"sendStatus\":\"SENT\",\"signedAt\":\"2026-01-28T22:44:02.948Z\",\"signingOrder\":1,\"signingStatus\":\"SIGNED\",\"templateId\":null,\"token\":\"SIGNING_TOKEN\"},{\"authOptions\":{\"accessAuth\":null,\"actionAuth\":null},\"documentDeletedAt\":null,\"documentId\":10,\"email\":\"signer1@documenso.com\",\"expired\":null,\"id\":51,\"name\":\"Signer 1\",\"readStatus\":\"OPENED\",\"rejectionReason\":null,\"role\":\"SIGNER\",\"sendStatus\":\"SENT\",\"signedAt\":\"2026-01-28T22:44:02.948Z\",\"signingOrder\":2,\"signingStatus\":\"SIGNED\",\"templateId\":null,\"token\":\"SIGNING_TOKEN\"}],\"authOptions\":null,\"completedAt\":\"2026-01-28T22:44:02.948Z\",\"createdAt\":\"2026-01-28T22:44:02.948Z\",\"deletedAt\":null,\"documentDataId\":\"hs8qz1ktr9204jn7mg6c5dxy0\",\"documentMeta\":{\"allowDictateNextSigner\":false,\"dateFormat\":\"MM/DD/YYYY\",\"distributionMethod\":\"EMAIL\",\"drawSignatureEnabled\":true,\"emailSettings\":null,\"id\":\"doc_meta_123\",\"language\":\"en\",\"message\":\"Hello, please review and sign this document.\",\"password\":null,\"redirectUrl\":null,\"signingOrder\":\"PARALLEL\",\"subject\":\"Please sign this document\",\"timezone\":\"UTC\",\"typedSignatureEnabled\":true,\"uploadSignatureEnabled\":true},\"externalId\":null,\"formValues\":null,\"id\":10,\"recipients\":[{\"authOptions\":{\"accessAuth\":null,\"actionAuth\":null},\"documentDeletedAt\":null,\"documentId\":10,\"email\":\"signer2@documenso.com\",\"expired\":null,\"id\":50,\"name\":\"Signer 2\",\"readStatus\":\"OPENED\",\"rejectionReason\":null,\"role\":\"VIEWER\",\"sendStatus\":\"SENT\",\"signedAt\":\"2026-01-28T22:44:02.948Z\",\"signingOrder\":1,\"signingStatus\":\"SIGNED\",\"templateId\":null,\"token\":\"SIGNING_TOKEN\"},{\"authOptions\":{\"accessAuth\":null,\"actionAuth\":null},\"documentDeletedAt\":null,\"documentId\":10,\"email\":\"signer1@documenso.com\",\"expired\":null,\"id\":51,\"name\":\"Signer 1\",\"readStatus\":\"OPENED\",\"rejectionReason\":null,\"role\":\"SIGNER\",\"sendStatus\":\"SENT\",\"signedAt\":\"2026-01-28T22:44:02.948Z\",\"signingOrder\":2,\"signingStatus\":\"SIGNED\",\"templateId\":null,\"token\":\"SIGNING_TOKEN\"}],\"source\":\"DOCUMENT\",\"status\":\"COMPLETED\",\"teamId\":null,\"templateId\":null,\"title\":\"documenso.pdf\",\"updatedAt\":\"2026-01-28T22:44:02.948Z\",\"userId\":1,\"visibility\":\"EVERYONE\"},\"webhookEndpoint\":\"https://app.windmill.dev/api/w/terrace-f-club/capture_u/http/flow/f.Membership.documenso_document_completed/f/Membership/documenso\"},\"createdAt\":\"2026-01-28T22:44:03.641Z\",\"webhookEndpoint\":\"https://app.windmill.dev/api/w/terrace-f-club/capture_u/http/flow/f.Membership.documenso_document_completed/f/Membership/documenso\"}",
#     "webhookEndpoint": "https://app.windmill.dev/api/w/terrace-f-club/capture_u/http/flow/f.Membership.documenso_document_completed/f/Membership/documenso"
# }