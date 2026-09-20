# Retained photos — setup, and what it does and does not give you

Reeve can keep the original photo behind a memory. This document covers why,
what the code guarantees, what it cannot guarantee, and exactly what you have to
set up.

**It is off by default and stays off until you configure a bucket.**

---

## Why it exists

The image embedding on each Episode is a one-way fingerprint. It is excellent at
*"find the photo that looks like this"* and incapable of *"how many people were
in it?"*. Your butter chicken photo is ~142 KB; its embedding is ~4 KB, and the
difference is not recoverable — it is a projection, not a compression.

So there are two different capabilities, and only one of them needs the original:

| Capability | Needs the photo? |
|---|---|
| Find photos by words | No — the embedding does it |
| Find photos by another photo | No — the embedding does it |
| Answer a question about what is *in* a photo | **Yes** |

That third row is the only reason this feature exists. If you do not need it,
leave it off and you keep a materially simpler privacy posture.

---

## What happens when it is on

1. `store_memory(..., image_url=…)` describes the photo, embeds it, **and**
   uploads the original to S3 under a key derived from a hash of the speaker.
2. The key — not the bytes — is recorded on the Episode as `image_key`.
3. At query time, when the image lane matches a photo (or the user attached one),
   the retained originals are fetched and shown to the **vision model**, which
   writes the answer.

Step 3 routes to `BEDROCK_VISION_MODEL_ID`, not the main chat model: Mistral
Large is text-only and could not see the photos at all.

---

## Setup

### 1. Bucket

Create it in **ap-south-1 (Mumbai)** unless you have a reason not to, and block
public access:

```bash
aws s3api create-bucket --bucket reeve-images-prod \
  --region ap-south-1 \
  --create-bucket-configuration LocationConstraint=ap-south-1

aws s3api put-public-access-block --bucket reeve-images-prod \
  --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

aws s3api put-bucket-encryption --bucket reeve-images-prod \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'
```

Deny any non-TLS access:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "DenyInsecureTransport",
    "Effect": "Deny",
    "Principal": "*",
    "Action": "s3:*",
    "Resource": ["arn:aws:s3:::reeve-images-prod", "arn:aws:s3:::reeve-images-prod/*"],
    "Condition": {"Bool": {"aws:SecureTransport": "false"}}
  }]
}
```

### 2. IAM for the EC2 instance role

Least privilege — no `s3:ListAllMyBuckets`, no other bucket:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::reeve-images-prod/*"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket", "s3:PutLifecycleConfiguration"],
      "Resource": "arn:aws:s3:::reeve-images-prod"
    }
  ]
}
```

`s3:ListBucket` is required — erasure sweeps by prefix, and without it a purge
cannot enumerate what to delete.

### 3. Environment

```bash
IMAGE_STORE_ENABLED=true
IMAGE_STORE_BUCKET=reeve-images-prod
IMAGE_STORE_REGION=ap-south-1
IMAGE_STORE_RETENTION_DAYS=0     # 0 = keep until the user deletes the memory
# IMAGE_STORE_KMS_KEY_ID=arn:aws:kms:...   # optional; SSE-S3 (AES256) without it
```

### Choosing a retention window

**`0` (indefinite) is what production runs**, because the feature's promise is
"ask anything about your photos, whenever". A finite window degrades *silently*:
on day N+1 a user asks what a sign said and gets a vague text answer with no
explanation, which reads as the product forgetting. Either keep the photo or
don't — the middle is the worst of both.

Indefinite retention is defensible under DPDP's storage limitation precisely
because the purpose requires it: you cannot answer new questions about a picture
you threw away. What makes it lawful is the rest — a notice that says photos are
kept until deleted, and deletion that actually works (verified against live S3:
erasing a memory removes the object).

Set a positive number instead if you want a hard cap. **The number must match the
S3 lifecycle rule** — the bucket enforces expiry independently of this variable,
so changing one without the other silently does nothing (or deletes photos you
meant to keep).

What survives expiry either way: the description, the image embedding, and the
whole graph. Text search, colour questions, and text→image / image→image search
keep working forever. Only re-reading the actual pixels stops.

---

## What the code guarantees

- **Off unless configured.** No bucket, no retention. Ever.
- **Encrypted** at rest (SSE-S3 or SSE-KMS) and in transit.
- **Keys disclose nothing.** The prefix is `sha256(speaker)`; a bucket listing
  shows no account id and no namespace name. The raw speaker embeds a Google uid.
- **Cross-tenant reads are refused.** `get_image` re-derives the prefix from the
  requesting speaker, so a stale or crafted key cannot read another tenant's
  photo even if it reaches the function.
- **Erasure covers the bytes.** `clear_memory` sweeps by prefix *before* deleting
  the graph, paginated. Sweeping by prefix rather than by the keys on the
  episodes is deliberate: it also reclaims photos whose write was cancelled
  before an episode ever existed, which nothing else could find.
- **Failed writes clean up after themselves.** A write that exhausts its retries
  deletes the photo it already uploaded.
- **Deletion failures are loud.** Logged at ERROR and reported in
  `images_deleted`; a purge that could not list returns 0 rather than claiming
  success.
- **Storage limitation.** An S3 lifecycle rule expires objects after
  `IMAGE_STORE_RETENTION_DAYS`. Descriptions and embeddings are untouched, so
  recall keeps working after the photo is gone.
- **Retention never breaks a write.** A store outage costs the ability to
  re-read that photo, never the memory itself.

## What the code does not give you

These are organisational, and they are the part DPDP actually enforces against:

- A **consent notice** and purpose statement covering photo retention
- A **privacy policy** that says you store images, where, and for how long
- A **grievance officer** and a route for Data Principal requests
- A **breach notification** process
- A **DPIA**, if you reach Significant Data Fiduciary thresholds

## One gap you should know about

`AWS_REGION` (default `us-west-2`) is where **Bedrock** runs, and it is separate
from where photos are *stored*. Every image is sent to that region to be
described and embedded — this was true before this feature existed and is
unchanged by it. Putting the bucket in Mumbai does not, on its own, keep Indian
users' photos in India.

DPDP §16 uses a negative list (transfers are permitted except to countries the
government notifies, and no list has been notified), so this is a
disclosure-and-consent matter rather than a prohibition. If you want processing
in India too, move `AWS_REGION` to `ap-south-1` and confirm your vision and
embedding models are available there first.

*Engineering input, not legal advice — have counsel review the posture before you
switch retention on for real users.*

---

## Turning it off again

Set `IMAGE_STORE_ENABLED=false`. New memories stop retaining originals
immediately. Photos already stored are **not** removed by that flag — they expire
on the retention clock, or you can purge the prefix directly:

```bash
aws s3 rm s3://reeve-images-prod/memories/ --recursive
```

Existing memories keep working: the descriptions and embeddings are unaffected,
so only the ability to re-read those photos is lost.
