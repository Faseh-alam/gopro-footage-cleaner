# SD Card Offloader

24/7 server tool: plug labeled GoPro SD cards → copy task folders to dual removable SSDs → optional AWS S3 sync. Multi-card parallel transfers with live progress and resume.

## What it copies

From each card:

```text
C1234/
  DCIM/
    100GOPRO/
      <task-folder>/*.MP4
```

Onto SSD:

```text
<SSD>/Batches/batch 6/C1234/<task-folder>/*.MP4
```

Skips `.LRV`, `.THM`, and other non-MP4 junk.

## Quick start

### Windows

1. Install Python 3.10+ and for AWS preferably **[s5cmd](https://github.com/peak/s5cmd)** (fast) plus [AWS CLI v2](https://aws.amazon.com/cli/) credentials via `aws configure`
2. Double-click `run.bat`
3. Browser opens `http://127.0.0.1:8877`

### Mac

```bash
cd sd_offloader
chmod +x run.sh
./run.sh
```

## Daily workflow

1. Plug both removable SSDs
2. Open the UI → **Refresh drives & batches**
3. Pick **SSD 1** and **SSD 2**
4. **Batch on SSDs** — select an existing batch already on the drives (e.g. `batch 3` from home), or **+ Create new batch…**
5. Choose mode:
   - **SSD only** — free cards fast; upload to AWS later from the office
   - **SSD + AWS** — when each card finishes, a **Command Prompt** runs **s5cmd** (or `aws s3 sync` fallback) with auto-retries. The web UI re-attaches and shows size / speed / ETA
6. Paste S3 folder URI (not keys), e.g. `s3://your-bucket/footage/`
7. **Start SD → SSD for this batch** — continues dumping cards into that batch (UI shows each card’s live transfer)
8. **Upload this batch to AWS (CMD)** — opens CMD (survives server restart) **and** shows live progress. Failed transfers auto-retry; use **Restart** in the job card if needed. After upload, **Verify sizes** compares local vs S3; only then use **Delete local** if you want to free the SSD
9. Plug SD cards — parallel copy with live MB/s / ETA; completed cards are verified, task folders wiped, ejected

### Office resume example (batch 3 dumped at home, no internet)

1. On the server: pick SSDs → select **batch 3** from the list  
2. Start SD → SSD if more cards still need dumping  
3. Click **Upload this batch to AWS (CMD)** — watch progress on the page; **Restart** resumes missing files (skips what’s already on S3)  

If SSD 1 fills up mid-batch, new cards spill to SSD 2 under the **same** batch folder name. AWS still syncs everything into one `…/batch 6/` prefix.

## Resume after crash / unplug

A progress file is written on the SD card:

`C1234/.gopro_offload_progress.json`

Replug the card (same batch session) and it continues from unfinished files.

## AWS login (one-time on the server)

**Do not put Access Keys in this app.** Use AWS CLI:

```bash
aws configure
```

Enter:

- AWS Access Key ID  
- AWS Secret Access Key  
- Default region  
- Output: `json`

Keys are stored by AWS CLI:

| OS | Location |
|----|----------|
| Windows | `C:\Users\<you>\.aws\credentials` |
| Mac | `~/.aws/credentials` |

Test:

```bash
aws s3 ls s3://your-bucket/footage/
```

In the offloader UI, only set:

```text
s3://your-bucket/footage/
```

Then click **Test AWS connection** — it uploads (and removes) an empty `_offloader_connection_test.txt` using your `aws configure` credentials. No keys go in any app config file.

The app runs `aws s3 sync` into `s3://your-bucket/footage/batch 6/…`.

### Upload later (SSD-only mode)

After cards are dumped, click **Upload batch to AWS now**. It syncs `Batches/batch 6` from both SSDs into the same S3 batch folder.

## IAM tip

Give the IAM user at least:

- `s3:ListBucket` on the bucket  
- `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` on `your-bucket/footage*`

## Ports

Default: `8877`  
Override: `SD_OFFLOADER_PORT=8899`

## Safety notes

- Wipe/eject happens only after size verification  
- Only transferred task folders under `DCIM/…GOPRO` are deleted on the card  
- After upload the UI compares local vs S3 sizes; **Delete local** is optional and only enabled when verified
- Config: `s5cmd_numworkers` (default 20 — used only after plain sync fails), `aws_upload_retries` (default 5) in `config.json`
