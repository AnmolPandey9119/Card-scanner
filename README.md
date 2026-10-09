# 📇 Visiting Card Scanner

Card ki **live photo** lo ya images **upload** karo (front + back). App saari details nikaal kar ek Excel bana deta hai.

## Features
- **📱 Phone scan (mobile ke liye best):** phone ka original rear camera khulta hai. "Take Photo" chuno, front → back → save, phir agla card
- 📷 Browser camera: seedha browser ka camera (laptop par theek, kuch phones par selfie camera khul sakta hai)
- 📁 Bulk upload: bahut saari images ek saath, front/back auto-pair
- Mobile friendly: bade buttons, chhoti screen ke hisaab se layout
- Batch me kai cards, parallel reading (tez)
- Result table me edit kar sakte ho, phir Excel download
- Optional password login (deploy ke liye)
- Excel columns: Name, Designation, Company, Mobile, Phone / Landline, Email, Website, Address, City, State, Pincode, Country, LinkedIn / Social, Services / Products, Other Notes, Front File, Back File

---

## A) Apne computer par chalana (Windows)
1. Python 3.10+ install karo (python.org, "Add to PATH" tick karo).
2. Folder me `api_key.txt` banao, usme sirf apni Anthropic API key paste karo (console.anthropic.com se milti hai).
3. `run.bat` double-click karo. Mac/Linux: `bash run.sh`.

---

## B) Deploy karna (taaki aap aur sir kisi bhi device se use kar sakein)

### Option 1: Streamlit Community Cloud (sabse easy, free)
1. Is folder ko ek **GitHub repo** me push karo (private bhi chalega). `api_key.txt` aur `secrets.toml` push NAHI hote, `.gitignore` handle karta hai.
2. share.streamlit.io par GitHub se login karo, **New app** pe click karo, apni repo choose karo, main file `app.py` rakho.
3. Deploy se pehle **Advanced settings → Secrets** me ye paste karo:
   ```
   ANTHROPIC_API_KEY = "sk-ant-..."
   APP_PASSWORD = "koi-strong-password"
   ```
4. Deploy dabao. Aapko ek link milega (https://....streamlit.app). Wo link aur password sir ko bhej do.

### Option 2: Render / koi bhi Docker host
Repo me `Dockerfile` already hai. Render par **New → Web Service** se repo connect karo (Docker environment) aur Environment Variables me `ANTHROPIC_API_KEY` aur `APP_PASSWORD` set karo.

---

## Zaroori baatein
- **Password zaroor set karo.** Bina password ke link jiske paas hoga wo aapki API key ka paisa kharch karega.
- Anthropic console me **monthly spend limit** laga do, safe rehne ke liye.
- Phone par hamesha **📱 Phone scan** tab use karo: ye phone ka original camera kholta hai, quality sabse achhi aati hai.
- "Browser camera" sirf **HTTPS** (deployed link) ya localhost par chalta hai.
- Streamlit Community Cloud ka free app kuch der use na hone par sleep ho jata hai; pehli baar kholne par 30-60 second lag sakte hain. Event se pehle ek baar kholke check kar lena.
- Photo ke saath sirf card ki details Anthropic API ko jaati hain; app khud kuch store nahi karta, tab band hote hi batch/results chale jate hain. Isliye Excel download karna na bhoolo.
- Columns badalne ho to `app.py` ke upar `HEADERS` edit karo.
