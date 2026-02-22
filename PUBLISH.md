# How to Publish PLC·SIM to GitHub

Follow these steps on your local machine. Requires [Git](https://git-scm.com/) and a [GitHub account](https://github.com).

---

## Step 1 — Create the GitHub Repository

1. Go to https://github.com/new
2. Fill in:
   - **Repository name**: `plc-sim`
   - **Description**: `SAP EWM MFS Device Simulator — Python GUI`
   - **Visibility**: Public or Private (your choice)
   - ❌ Do NOT tick "Add a README file" (we already have one)
3. Click **Create repository**
4. Copy the repository URL shown — it will look like:
   `https://github.com/<your-username>/plc-sim.git`

---

## Step 2 — Set Up Your Local Project

Open a terminal and run:

```bash
# Navigate to the folder where you saved the project files
cd /path/to/plc_sim

# Initialise git
git init

# Stage all files
git add .

# First commit
git commit -m "Initial commit — PLC·SIM v2.0"
```

---

## Step 3 — Push to GitHub

```bash
# Link to your GitHub repository (replace with your actual URL)
git remote add origin https://github.com/<your-username>/plc-sim.git

# Set main branch name
git branch -M main

# Push
git push -u origin main
```

You will be prompted for your GitHub credentials.
If you use 2FA, use a **Personal Access Token** instead of your password:
→ https://github.com/settings/tokens/new (select `repo` scope)

---

## Step 4 — Verify

Go to `https://github.com/<your-username>/plc-sim` — you should see all files and the rendered README.

---

## Optional — Add Topics & Description on GitHub

On your repository page:
1. Click the ⚙️ gear icon next to **About**
2. Add a description: `SAP EWM MFS PLC/device simulator — real TCP, 128-byte telegram protocol`
3. Add topics: `sap`, `ewm`, `mfs`, `plc`, `simulator`, `python`, `tcp`, `industrial`

---

## File Structure

```
plc-sim/
├── plc_sim.py        ← main application
├── requirements.txt  ← pip dependencies
├── README.md         ← project documentation
├── SECURITY.md       ← vulnerability audit report
├── LICENSE           ← MIT licence
└── .gitignore        ← excludes __pycache__, exports, etc.
```
