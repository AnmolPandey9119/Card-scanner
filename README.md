# 📇 Visiting Card Scanner

Card ki **live photo** lo ya images **upload** karo (front + back). App saari details nikaal kar ek Excel bana deta hai. **100% free: OCR local chalta hai (RapidOCR), koi API key / internet / paisa nahi.**

## Features
- **📱 Phone scan (mobile ke liye best):** phone ka original rear camera khulta hai. "Take Photo" chuno, front → back → save, phir agla card
- 📷 Browser camera: seedha browser ka camera (laptop par theek; mobile par back camera apne aap khulta hai)
- 📁 Bulk upload: bahut saari images ek saath, front/back auto-pair
- Mobile friendly: bade buttons, chhoti screen ke hisaab se layout
- **Live processing:** card (front + back) save/add karte hi wo background me read hona shuru ho jata hai, aap agla card le sakte ho. Status (ready / reading / failed) live dikhta hai
- Har card ka row seedha Excel me judta jata hai, to **Export Excel** dabate hi file turant download hoti hai (koi alag "Extract" step nahi)
- Result table me edit kar sakte ho (galat row ke liye Delete tick), failed cards ke liye Retry button
- Optional password login (deploy ke liye)
- Excel columns: Name, Designation, Company, Mobile, Phone / Landline, Email, Website, Address, City, State, Pincode, Country, LinkedIn / Social, Services / Products, Other Notes, Front File, Back File

---

## A) Apne computer par chalana (Windows)
1. Python 3.10+ install karo (python.org, "Add to PATH" tick karo).
2. Kuch aur setup nahi chahiye (API key nahi lagti).
3. `run.bat` double-click karo. Mac/Linux: `bash run.sh`.

---

## B) Render par deploy karna (taaki aap aur sir kisi bhi device se use kar sakein)
1. Is folder ko ek **GitHub repo** me push karo (private bhi chalega). `api_key.txt` aur `secrets.toml` push NAHI hote, `.gitignore` handle karta hai.
2. render.com par GitHub se login karo → **New → Web Service** → apni repo chuno.
3. **Language/Runtime: Docker** rakho (repo me `Dockerfile` already hai, port Render ke `PORT` se khud set ho jata hai).
4. **Environment Variables** me ye daalo:
   ```
   APP_PASSWORD   = koi-strong-password
   ```
5. (Optional) Settings me **Health Check Path** `/_stcore/health` rakh do.
6. Deploy dabao. Aapko ek link milega (https://....onrender.com). Wo link aur password sir ko bhej do.

---

## Zaroori baatein
- Ab koi API key nahi chahiye. Sab kuch server/computer par hi hota hai, card ka data kahin bahar nahi jata.
- OCR rule-based hai, to **table check karke edit zaroor karo**, khaaskar Name/Company/Address. Photo saaf, seedhi aur achhi roshni me lo to accuracy best aati hai.
- Abhi English/Latin text padhta hai. Hindi ke liye alag OCR model lagana padega.
- Password zaroor set karo (`APP_PASSWORD`) agar link public hai.
- Render free plan (512MB RAM) me chalta hai; `WORKERS` app.py me 2 hai, RAM kam pade to 1 kar do. Pehla card load hone me kuch second lagte hain (OCR model load).
- Phone par **📱 Phone scan** tab use karo (original camera, best quality). "Browser camera" sirf HTTPS ya localhost par chalta hai.
- Cards/Excel server memory me rehte hain; refresh/restart par chale jate hain, to beech-beech me Export Excel karte raho.
- Columns badalne ho to `app.py` ke upar `HEADERS` edit karo.
