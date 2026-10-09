# 📇 Visiting Card Scanner

Card ki **live photo** lo ya images **upload** karo (front + back). App saari details nikaal kar ek Excel bana deta hai.

## Features
- **📱 Phone scan (mobile ke liye best):** phone ka original rear camera khulta hai. "Take Photo" chuno, front → back → save, phir agla card
- 📷 Browser camera: seedha browser ka camera (laptop par theek, kuch phones par selfie camera khul sakta hai)
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
2. Folder me `api_key.txt` banao, usme sirf apni **Gemini API key** paste karo (free, aistudio.google.com/apikey se milti hai).
3. `run.bat` double-click karo. Mac/Linux: `bash run.sh`.

---

## B) Render par deploy karna (taaki aap aur sir kisi bhi device se use kar sakein)
1. Is folder ko ek **GitHub repo** me push karo (private bhi chalega). `api_key.txt` aur `secrets.toml` push NAHI hote, `.gitignore` handle karta hai.
2. render.com par GitHub se login karo → **New → Web Service** → apni repo chuno.
3. **Language/Runtime: Docker** rakho (repo me `Dockerfile` already hai, port Render ke `PORT` se khud set ho jata hai).
4. **Environment Variables** me ye daalo:
   ```
   GEMINI_API_KEY = AIza...
   APP_PASSWORD   = koi-strong-password
   ```
5. (Optional) Settings me **Health Check Path** `/_stcore/health` rakh do.
6. Deploy dabao. Aapko ek link milega (https://....onrender.com). Wo link aur password sir ko bhej do.

---

## Zaroori baatein
- **Password zaroor set karo.** Bina password ke link jiske paas hoga wo aapki API key ka free quota khatam kar dega.
- Free tier me requests/minute aur requests/day ki limit hoti hai. App rate-limit (429) par khud retry karta hai, to bade batch thode dheere chalenge. Limits AI Studio me dekh lo.
- Free tier me Google aapka data apne products sudharne ke liye use kar sakta hai. Cards me logon ke phone/email hote hain, to sensitive/client data ke liye paid key use karna better hai.
- Model: app `gemini-3.8-flash` use karta hai, aur agar Google koi model band kar de (404) to khud agla (`gemini-3.5-flash`, `gemini-3.1-flash-lite`) try kar leta hai. Apna model chahiye to Render env me `GEMINI_MODEL = model-name` set karo (kai ho to comma se alag karo).
- Phone par hamesha **📱 Phone scan** tab use karo: ye phone ka original camera kholta hai, quality sabse achhi aati hai.
- "Browser camera" sirf **HTTPS** (deployed link) ya localhost par chalta hai.
- **Render free plan:** 15 minute koi traffic na aaye to service sleep ho jati hai, aur dobara kholne par ek minute tak lag sakta hai. Event se pehle ek baar kholke jaga lo. Free service restart/sleep hone par server ki memory aur files dono jaati hain, to sab cards/Excel chale jayenge.
- Photo ke saath sirf card ki details Gemini API ko jaati hain; app khud kuch store nahi karta. Cards aur Excel server ki memory me is session tak rehte hain. Tab refresh/band karne ya Render ke sleep/restart par chale jate hain. Isliye beech-beech me Export Excel karte raho. Export dabane par sirf wahi cards aayenge jo tab tak read ho chuke hain.
- Columns badalne ho to `app.py` ke upar `HEADERS` edit karo.