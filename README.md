# Campus Lost & Found AI Matcher

**Find Lost Items. Connect Students. Verify. Recover.**

A complete Flask website: students report lost and found items, an AI suggests matches, finder and owner chat in the app, and college staff verify ownership and record the handover in a history log. Finder and owner can also hand the item over to each other directly.

The database starts **empty**. There are no demo users, items, matches, messages or receipts. The only automatic record is the staff admin account.

---

## 1. Install and run on Windows

1. **Install Python 3.10 or newer** from https://www.python.org/downloads/ . On the first installer screen, tick **"Add python.exe to PATH"**.
2. Open the project folder, click the address bar, type `cmd` and press Enter.
3. Create a virtual environment and install packages:

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

4. Start the website:

```
python app.py
```

5. Open **http://127.0.0.1:5000** in your browser. Stop the server with `Ctrl + C`.

**Mac/Linux:** use `python3 -m venv venv`, `source venv/bin/activate`, then the same `pip` and `python3 app.py`.

### Creating the database
Nothing to do. When the app starts it creates `instance/database.db`, all tables, and the admin account. To start again from zero, stop the app and delete `instance/database.db` and the pictures in `static/uploads/`.

---

## 2. How people sign in

**Home page** shows the project name and two options: **User** and **Admin**.

**User (student):** enter only **name + USN**. A new USN creates the account automatically. If the USN already exists, the name must match what was entered the first time. There is no password, so keep this for the demo only. For real use add a password or college login.

**Admin (college office):** Admin ID `admin`, password `admin` at `/admin/login`.
* The account is created automatically on first start and the password is stored as a hash.
* Every `/admin/...` page is protected on the server. Students get a "no access" page.
* **This default password is for the local hackathon demo only. Change it (Profile page) before any real deployment.**

---

## 3. How it works (and how to test it, about 10 minutes)

Use two browsers, or one normal window and one private window, so two students are logged in at once.

1. Open http://127.0.0.1:5000. Choose **User**. Sign in as Student A (name + USN).
2. Student A presses **I lost an item** and fills the form (USN is filled in from the account). Add a photo.
3. Student B (second window) presses **I found an item** with a similar item. Tick "already deposited at the college office".
4. **If the AI match is 20% or more, a chat opens automatically** and both students get an alert. Open **Messages**: your bubbles are blue, the other person's are green with their name.
5. **Privacy:** a third student who has no matching item cannot see these items at all (they get "page not found"), and cannot claim them. Nobody has a list of all found items.
6. **Admin:** choose Admin on the home page. Go to **Receive item**, enter the Item ID, finder USN, Officer ID and storage place, then press *Mark Reached Office*.
7. Student A opens the matched found item and presses **Claim this item** (only possible for the owner whose lost report matched). 
8. Admin: **Claims** → open the claim → *Verify Ownership* → *Approve Claim*. Steps cannot be skipped.
9. Admin: **Give item** → enter receiver USN (must equal the claimant) and Officer ID → *Confirm Handover* → tick the box → *Mark Received by Owner*. The handover appears in **Handover history** (record HR-2026-001). There is no print or receipt page.
10. Admin: **Records** → search by Item ID or any USN → **Full record**. This is the saved trail for the item: finder, who received it and when, every claim with USN and details, who it was given to, the handover record, and a full timeline. A "wrong-claim check" compares the receiver USN with the owner USN on the lost report. **Download CSV** exports all records.

To start again from zero: stop the app, delete `instance/database.db` and the photos in `static/uploads/`. (If you used an older version, the app keeps the old file as `database.db.old` and makes a fresh one.)

---

## 4. Project structure

```
campus-lost-found/
├── app.py                 Flask app: routes, database, security, workflow rules
├── requirements.txt
├── README.md
├── ai/
│   ├── __init__.py
│   └── matching.py        TF-IDF + cosine similarity, image hash similarity
├── templates/             18 pages + shared pieces (_macros.html, _match_cards.html, base.html)
├── static/
│   ├── css/style.css
│   ├── js/script.js       menu, photo preview, live chat
│   ├── images/
│   └── uploads/           uploaded photos (re-saved as clean JPEGs)
└── instance/
    └── database.db        created automatically (SQLite)
```

### Pages
Home (User / Admin), User sign-in, User home, Report Lost, Report Found, My reports, Item Details, AI Match Results, Messages, Alerts, Claim, Profile, **Admin:** Login, Dashboard, All Reports, AI Matches, Receive item, Claims (verification), Give item (handover and history), Records, Disputes.

### Database tables
`users`, `items`, `messages`, `office_items`, `claims`, `handovers`, `notifications` (as requested) plus `matches`, `enquiries`, `exchanges`, `disputes`, `audit_log`. They use foreign keys, UNIQUE and CHECK constraints (for example, an item status must be one of the 11 allowed values).

---

## 4b. New in this version

* **Direct exchange (finder to owner):** in a chat, the finder presses **I handed it to the owner**. The owner then ticks the box and presses **Confirm I received it** (or **Not received yet**). When confirmed, both reports become *Returned to Owner*, the chat closes, and staff can see it under **Give item → Direct exchanges**. It is not offered once the item is at the college office (use a claim then).
* **Message pop-ups:** when you are on any page except Messages, a small pop-up shows the sender and text of a new chat message (checked every 5 seconds). Click it to open the chat.
* **No category for students:** the category box is gone from the report form and from student pages. The app guesses a category from the words in the report so matching still works. Staff still see and filter by it.
* **AI description from a photo:** choose a photo on the report form and the description (plus name, colour, brand and features if they are empty) is filled in. Text you typed yourself is never overwritten.
  * **With Claude (best):** set an API key before starting the app. Windows: `set ANTHROPIC_API_KEY=your-key` then `python app.py`. Mac/Linux: `export ANTHROPIC_API_KEY=your-key`. Optional: `ANTHROPIC_MODEL` (default `claude-haiku-5-5`).
  * **Without a key:** a basic built-in reader describes only the main colours and shape. It cannot recognise what the object is.
  * The photo is sent to Anthropic only when a key is set. Mention this to students if you deploy it.
* **Receipts removed:** no print page. Completed handovers appear in the Handover history table and in each item's Full record.

---

## 5. How the AI matching works

* **Text:** each report is turned into one string of name, category, color, brand, distinctive features, description and location (important fields repeated for more weight). `TfidfVectorizer` + `cosine_similarity` compare it with every report of the opposite type. TF-IDF is fitted on all reports, so common words ("bottle") count less than rare ones ("smiley sticker").
* **Category check:** if categories differ, the text score is halved.
* **Photos:** when both reports have a photo, a perceptual hash (ImageHash `phash`) is compared and combined as 70% text + 30% photo. Without photos, text alone is used.
* **Threshold:** only matches of **20% or more** are kept and open a chat. Change `MIN_SCORE` in `ai/matching.py`.
* **Recalculation:** matches are recomputed when a report is created, edited, or registered at the office. Admin can press **Recalculate all**.
* **Scores are never hardcoded** and never approve anything.

**Limits (shown in the app too):** image hashes are not object recognition, and angle or lighting changes lower the score. Identical college-issued bottles can look the same, so matching also uses stickers, scratches and initials, and staff verify against **private details** that are hidden from listings. Found items' distinctive features are hidden from other students for the same reason.

---

## 6. Security and privacy

* Admin password hashed (PBKDF2-SHA256); session cookies are HTTP-only; failed logins are throttled. Students sign in with name + USN only (demo simplification).
* Role checks on the server for every student and admin route; chat and claims check who the user is.
* CSRF token on every POST form and chat request.
* All SQL uses parameters. Templates auto-escape text. Chat messages are inserted with `textContent`.
* Photo uploads: extension check, real image check with Pillow, size limit (6 MB), random file name, metadata removed.
* **Items are private:** a student can open an item only if it is their own or the AI matched it with their own report. There is no public list. Only a student whose lost report matched a found item can claim it.
* USNs and Officer IDs are never shown to other students. Chats show names only. Admins see USNs.
* Workflow guards in the backend: a handover is impossible before **verify, then approve**, and "Received by Owner" is impossible before **Confirm Handover**. The receiver USN must equal the verified claimant's USN.
* A rejected claim is recorded neutrally and is never treated as an accusation.

---

## 7. Notes on simulated or simplified parts

* **Notifications** are in-app only (no email/SMS).
* **Fonts** load from Google Fonts when online and fall back to system fonts offline.
* **ImageHash fallback:** if `ImageHash` is not installed, a small built-in average-hash (Pillow) is used. `pip install -r requirements.txt` installs the real one.
* **College email domain:** set `COLLEGE_EMAIL_DOMAIN = "yourcollege.edu"` at the top of `app.py` to accept only that domain.
* Chat refreshes every 4 seconds (simple polling) instead of WebSockets.
* This is a single-user local demo server. For real use: change the admin password, use HTTPS, and run behind a production server.

## 8. Troubleshooting

| Problem | Fix |
|---|---|
| `python` not recognized | Reinstall Python and tick "Add to PATH", or try `py app.py` |
| `ModuleNotFoundError` | Activate the venv (`venv\Scripts\activate`) and run `pip install -r requirements.txt` |
| "This USN is already registered under a different name" | Type the name exactly as the first time, or use a different USN |
| Port 5000 in use | Change `port=5000` at the bottom of `app.py` |
| "Your form expired" | Refresh the page and submit again |
| Want a clean slate | Stop the app, delete `instance/database.db` and files in `static/uploads/` |
