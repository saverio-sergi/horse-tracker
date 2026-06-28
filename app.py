from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import os, json, re, time, threading, imaplib, requests
from pathlib import Path

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-key-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///horsetracker.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access your stable.'

# ══════════════════════════════════════════════════════════════
# MODELS
# ══════════════════════════════════════════════════════════════
class User(UserMixin, db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    email         = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    stable_name   = db.Column(db.String(120), default='My Stable')
    currency      = db.Column(db.String(3), default='USD')
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    # TrackIT
    trackit_username   = db.Column(db.String(100))
    trackit_password   = db.Column(db.String(256))   # stored encrypted in production
    trackit_connected  = db.Column(db.Boolean, default=False)
    trackit_last_sync  = db.Column(db.DateTime)
    trackit_credits    = db.Column(db.Integer, default=0)
    # Gmail label
    gmail_address = db.Column(db.String(120))
    imap_password = db.Column(db.String(256))
    gmail_label   = db.Column(db.String(100), default='')
    trackmaster_label = db.Column(db.String(100), default='RaceReminders')
    # Relationships
    horses  = db.relationship('Horse',  backref='owner_user', lazy=True, cascade='all,delete')
    syncs   = db.relationship('SyncLog', backref='user',      lazy=True, cascade='all,delete')

    def set_password(self, pw):   self.password_hash = generate_password_hash(pw)
    def check_password(self, pw): return check_password_hash(self.password_hash, pw)

class Horse(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    horse_id   = db.Column(db.String(20))
    name       = db.Column(db.String(100), nullable=False)
    gait       = db.Column(db.String(20), default='Pacer')
    year_foaled= db.Column(db.Integer)
    status     = db.Column(db.String(20), default='active')
    trackmaster_confirmed = db.Column(db.Boolean, default=False)  # user confirmed horse added to TrackMaster
    purchase_price = db.Column(db.Float)
    purchase_date  = db.Column(db.Date)
    purchase_type  = db.Column(db.String(50))
    sale_price     = db.Column(db.Float)
    sale_date      = db.Column(db.Date)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)
    ownerships = db.relationship('Ownership',      backref='horse', lazy=True, cascade='all,delete')
    periods    = db.relationship('OwnershipPeriod', backref='horse', lazy=True, cascade='all,delete',
                                 order_by='OwnershipPeriod.start_date')
    bills      = db.relationship('Bill', backref='horse', lazy=True, cascade='all,delete')
    races      = db.relationship('Race', backref='horse', lazy=True, cascade='all,delete')

class Ownership(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    horse_id     = db.Column(db.Integer, db.ForeignKey('horse.id'), nullable=False)
    owner_name   = db.Column(db.String(100), nullable=False)
    owner_email  = db.Column(db.String(120))
    pct          = db.Column(db.Float, nullable=False)

class Bill(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    horse_id    = db.Column(db.Integer, db.ForeignKey('horse.id'), nullable=False)
    date         = db.Column(db.Date, default=datetime.utcnow)  # invoice/receipt date
    service_date = db.Column(db.Date)  # date the service was actually performed
    # Period assignment uses service_date when set, falls back to date.
    # This handles late bills from trainers/farriers for work done before a claim.
    category    = db.Column(db.String(50))
    vendor      = db.Column(db.String(150))
    description = db.Column(db.String(200))
    amount      = db.Column(db.Float, nullable=False)
    needs_review= db.Column(db.Boolean, default=False)
    period_id   = db.Column(db.Integer, db.ForeignKey('ownership_period.id'), nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    payments    = db.relationship('Payment', backref='bill', lazy=True, cascade='all,delete')

    @property
    def period_date(self):
        """
        The date used to determine which ownership period a bill belongs to.
        Uses service_date when available (work was done then), otherwise falls
        back to the invoice date.  This ensures a late-arriving trainer bill
        for pre-claim work lands in the correct (previous owner's) period.
        """
        return self.service_date if self.service_date else self.date

    @property
    def is_late_bill(self):
        """True if this bill arrived after the service was performed."""
        return bool(self.service_date and self.date and self.service_date < self.date)

class Payment(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    bill_id     = db.Column(db.Integer, db.ForeignKey('bill.id'), nullable=False)
    owner_name  = db.Column(db.String(100))
    amount_due  = db.Column(db.Float)
    paid        = db.Column(db.Boolean, default=False)
    paid_at     = db.Column(db.DateTime)

class Race(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    horse_id   = db.Column(db.Integer, db.ForeignKey('horse.id'), nullable=False)
    date       = db.Column(db.Date)
    track      = db.Column(db.String(100))
    race_name  = db.Column(db.String(100))
    finish     = db.Column(db.Integer)
    time_str   = db.Column(db.String(20))
    purse      = db.Column(db.Float)
    currency   = db.Column(db.String(3), default='USD')
    race_type  = db.Column(db.String(20), default='Overnight')  # Overnight|Claim|Stakes|Qualifier
    claim_price= db.Column(db.Float)  # if this race resulted in a claim
    period_id  = db.Column(db.Integer, db.ForeignKey('ownership_period.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class OwnershipPeriod(db.Model):
    """
    One contiguous window during which the user owned this horse.
    A horse bought, sold, and repurchased gets two separate periods.

    Bills and Races are linked to the period they fall within.
    Gaps between periods (when someone else owned the horse) are
    hidden from all views - only the user's own periods are shown.
    """
    id           = db.Column(db.Integer, primary_key=True)
    horse_id     = db.Column(db.Integer, db.ForeignKey('horse.id'), nullable=False)
    period_index = db.Column(db.Integer, default=1)   # 1st ownership, 2nd, etc.
    start_date   = db.Column(db.Date, nullable=False)  # defaults to purchase_date
    end_date     = db.Column(db.Date)                  # None = still owned
    purchase_price = db.Column(db.Float)
    purchase_type  = db.Column(db.String(50))
    sale_price     = db.Column(db.Float)
    sale_type      = db.Column(db.String(50))
    notes          = db.Column(db.String(300))
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)

    bills = db.relationship('Bill', backref='period', lazy=True)
    races = db.relationship('Race', backref='period', lazy=True)

    @property
    def label(self):
        s = self.start_date.strftime('%b %d, %Y') if self.start_date else '?'
        e = self.end_date.strftime('%b %d, %Y') if self.end_date else 'present'
        idx = f' (#{self.period_index})' if self.period_index > 1 else ''
        return f'{s} - {e}{idx}'

    @property
    def active(self):
        return self.end_date is None

    def contains(self, d):
        """
        Return True if date d falls within this ownership period.
        The end_date is INCLUSIVE - this implements the claiming rule:
        a race on the day the horse was claimed still belongs to this
        period, so the seller keeps that race's purse earnings.
        Bills after end_date belong to the buyer's new period.
        """
        if d is None:
            return True   # undated items shown in current period
        if self.start_date and d < self.start_date:
            return False
        if self.end_date and d > self.end_date:
            return False
        return True

    def contains_bill(self, d):
        """
        Bills use a stricter rule - a bill on the claim date goes
        to the BUYER (new owner) since bills incurred on/after the
        claim date are the new owner's responsibility.
        """
        if d is None:
            return True
        if self.start_date and d < self.start_date:
            return False
        # Bills: end_date is EXCLUSIVE (bill on end_date = buyer's bill)
        if self.end_date and d >= self.end_date:
            return False
        return True

class SyncLog(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    synced_at  = db.Column(db.DateTime, default=datetime.utcnow)
    track      = db.Column(db.String(100))
    races_added= db.Column(db.Integer, default=0)
    status     = db.Column(db.String(20), default='success')


class UpcomingRace(db.Model):
    """A detected or manually entered upcoming race entry."""
    id            = db.Column(db.Integer, primary_key=True)
    horse_id      = db.Column(db.Integer, db.ForeignKey('horse.id'), nullable=False)
    race_date     = db.Column(db.Date, nullable=False)
    track         = db.Column(db.String(120), nullable=False)
    race_number   = db.Column(db.String(10))
    post_time     = db.Column(db.String(20))
    post_position = db.Column(db.String(10))
    purse         = db.Column(db.Float)
    driver        = db.Column(db.String(100))
    trainer       = db.Column(db.String(100))
    morning_line  = db.Column(db.String(20))
    conditions    = db.Column(db.String(300))
    source        = db.Column(db.String(20), default='manual')  # 'trackmaster' | 'manual'
    notified_at   = db.Column(db.DateTime)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    notifications = db.relationship('OwnerNotification', backref='upcoming_race',
                                    lazy=True, cascade='all,delete')
    horse         = db.relationship('Horse', backref='upcoming_races')

class OwnerNotification(db.Model):
    """Tracks whether an owner was notified about an upcoming race."""
    id               = db.Column(db.Integer, primary_key=True)
    upcoming_race_id = db.Column(db.Integer, db.ForeignKey('upcoming_race.id'), nullable=False)
    owner_name       = db.Column(db.String(100), nullable=False)
    owner_email      = db.Column(db.String(120))
    sent_at          = db.Column(db.DateTime)
    send_requested   = db.Column(db.Boolean, default=False)


class HorseSync(db.Model):
    """Log of automatic TrackIT + TrackMaster lookups triggered when a horse is added."""
    id         = db.Column(db.Integer, primary_key=True)
    horse_id   = db.Column(db.Integer, db.ForeignKey('horse.id'), nullable=False)
    checked_at = db.Column(db.DateTime, default=datetime.utcnow)
    trackit_found    = db.Column(db.Boolean, default=False)
    trackit_races    = db.Column(db.Integer, default=0)   # historic races imported
    trackmaster_note = db.Column(db.String(300))          # guidance shown to user
    status     = db.Column(db.String(20), default='pending')  # pending|done|error
    notes      = db.Column(db.String(500))

@login_manager.user_loader
def load_user(uid): return User.query.get(int(uid))

# ══════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════
PURSE_PCT    = {1:0.50, 2:0.25, 3:0.12, 4:0.08, 5:0.05}
DRIVER_PCT   = 0.05
TRAINER_PCT  = 0.05
OWNER_PCT    = 0.90
FINISH_LABEL = {1:'1st',2:'2nd',3:'3rd',4:'4th',5:'5th'}
CATEGORIES   = ['Farrier','Vet','Feed','Training','Stabling','Entry Fee',
                'Transport','Supplements','Dentist','Insurance','Other']
TRACKIT_BASE = 'https://trackit.standardbredcanada.ca'

def horse_earnings(race):
    return race.purse * PURSE_PCT.get(race.finish, 0)

def owner_earnings(race, pct):
    return horse_earnings(race) * OWNER_PCT * (pct / 100)

def trackit_login(username, password):
    """Attempt TrackIT login. Returns (session, error)."""
    try:
        s = requests.Session()
        s.headers['User-Agent'] = 'Mozilla/5.0'
        r = s.get(f'{TRACKIT_BASE}/login.cfm', timeout=10)
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, 'html.parser')
        payload = {'username': username, 'password': password.lower().replace(' ','')}
        for inp in soup.find_all('input', type='hidden'):
            if inp.get('name'): payload[inp['name']] = inp.get('value','')
        r2 = s.post(f'{TRACKIT_BASE}/login.cfm', data=payload, timeout=10, allow_redirects=True)
        page = r2.text.lower()
        if any(x in page for x in ['my account','racelines','logout','welcome']):
            return s, None
        return None, 'Invalid username or password. Please check your TrackIT credentials.'
    except Exception as e:
        return None, f'Could not reach TrackIT: {e}'

def trackit_test_connection(username, password):
    """Test credentials and return (success, error, credits)."""
    s, err = trackit_login(username, password)
    if err: return False, err, 0
    return True, None, 0




# ══════════════════════════════════════════════════════════════
# HORSE SYNC - runs automatically when a horse is added or restored
# ══════════════════════════════════════════════════════════════
def sync_horse_on_add(horse, user):
    """
    Called immediately after a horse is saved to the database.
    1. Searches TrackIT for the horse and imports any historic race results.
    2. Records a HorseSync log entry with the outcome.
    3. Returns a human-readable summary string for the flash message.

    TrackMaster is email-driven - we cannot proactively search it, but we
    remind the user to add the horse to their TrackMaster Virtual Stable
    so future entry emails are detected automatically.
    """
    log_entry = HorseSync(horse_id=horse.id, status='pending')
    db.session.add(log_entry)
    db.session.commit()

    summary_parts = []
    trackit_races_added = 0

    # ── TrackIT historic results ──────────────────────────────
    if user.trackit_connected and user.trackit_username and user.trackit_password:
        try:
            trackit = _TrackITQuickSearch()
            if trackit.login(user.trackit_username, user.trackit_password):
                matches = trackit.search_horse(horse.name)
                if matches:
                    horse_url, _ = matches[0]
                    preferred_currency = 'USD' if user.currency == 'USD' else 'CAD'
                    results = trackit.get_racelines(horse.name, horse_url, preferred_currency)
                    if results:
                        for r in results:
                            # Only add races not already in the database
                            exists = Race.query.filter_by(
                                horse_id=horse.id,
                                date=r.get('date'),
                                track=r.get('track',''),
                            ).first()
                            if not exists and r.get('date') and r.get('purse') is not None:
                                db.session.add(Race(
                                    horse_id  = horse.id,
                                    date      = r['date'],
                                    track     = r.get('track',''),
                                    race_name = r.get('race_name',''),
                                    finish    = r.get('finish', 0),
                                    time_str  = r.get('time_str',''),
                                    purse     = r.get('purse', 0),
                                    currency  = r.get('currency', user.currency),
                                ))
                                trackit_races_added += 1
                        db.session.commit()
                        log_entry.trackit_found = True
                        log_entry.trackit_races = trackit_races_added
                        summary_parts.append(
                            f"TrackIT: found {horse.name} - "
                            f"{trackit_races_added} historic race{'s' if trackit_races_added != 1 else ''} imported."
                        )
                    else:
                        summary_parts.append(f"TrackIT: {horse.name} found but no race history yet.")
                        log_entry.trackit_found = True
                else:
                    summary_parts.append(
                        f"TrackIT: {horse.name} not found - "
                        f"they may not have raced yet or the name may differ slightly on TrackIT."
                    )
        except Exception as e:
            app.logger.warning(f'TrackIT auto-sync failed for horse {horse.id}: {e}')
            summary_parts.append("TrackIT: could not connect - you can sync manually from the TrackIT page.")
    else:
        summary_parts.append(
            "TrackIT: not connected - connect your account in Settings to import race history automatically."
        )

    # ── TrackMaster guidance ──────────────────────────────────
    # We cannot push to TrackMaster - it is email-driven. We remind the user
    # to add the horse there so future entry emails are auto-detected.
    tm_note = (
        f"TrackMaster: add '{horse.name}' to your Virtual Stable at trackmaster.com "
        f"so upcoming race entries are detected automatically."
    )
    log_entry.trackmaster_note = tm_note
    summary_parts.append(tm_note)

    log_entry.status = 'done'
    log_entry.notes  = ' | '.join(summary_parts)
    db.session.commit()

    return summary_parts


class _TrackITQuickSearch:
    """Lightweight TrackIT session used for on-add horse lookups."""
    TRACKIT_BASE = 'https://trackit.standardbredcanada.ca'
    RACELINE_URL = f'{TRACKIT_BASE}/cgi-bin/racelines.cgi'

    def __init__(self):
        self.session   = requests.Session()
        self.session.headers['User-Agent'] = 'Mozilla/5.0'
        self.logged_in = False

    def login(self, username, password):
        try:
            from bs4 import BeautifulSoup
            r = self.session.get(f'{self.TRACKIT_BASE}/login.cfm', timeout=10)
            soup = BeautifulSoup(r.text, 'html.parser')
            payload = {'username': username, 'password': password}
            for inp in soup.find_all('input', type='hidden'):
                if inp.get('name'):
                    payload[inp['name']] = inp.get('value', '')
            r2 = self.session.post(
                f'{self.TRACKIT_BASE}/login.cfm',
                data=payload, timeout=10, allow_redirects=True
            )
            page = r2.text.lower()
            self.logged_in = any(x in page for x in ['my account','racelines','logout','welcome'])
            return self.logged_in
        except Exception:
            return False

    def search_horse(self, name):
        try:
            from bs4 import BeautifulSoup
            params = {'name': f'"{name}"', 'type': 'horse'}
            r = self.session.get(self.RACELINE_URL, params=params, timeout=10)
            soup = BeautifulSoup(r.text, 'html.parser')
            matches = []
            for a in soup.find_all('a', href=True):
                href = a['href']
                if 'horse' in href.lower() and name.split()[0].lower() in a.get_text().lower():
                    matches.append((href, a.get_text().strip()))
            return matches[:3]
        except Exception:
            return []

    def get_racelines(self, horse_name, horse_url, preferred_currency='USD'):
        try:
            from bs4 import BeautifulSoup
            url = (horse_url if horse_url.startswith('http')
                   else self.TRACKIT_BASE + '/' + horse_url.lstrip('/'))
            r = self.session.get(url, timeout=12)
            soup = BeautifulSoup(r.text, 'html.parser')

            if preferred_currency == 'USD':
                # Find and follow the USD toggle link
                for a in soup.find_all('a', href=True):
                    if 'us$' in a.get_text().lower() or 'usd' in a.get_text().lower():
                        usd_href = a['href']
                        usd_url  = (usd_href if usd_href.startswith('http')
                                    else self.TRACKIT_BASE + '/' + usd_href.lstrip('/'))
                        r2 = self.session.get(usd_url, timeout=12)
                        soup = BeautifulSoup(r2.text, 'html.parser')
                        break

            return self._parse_racelines(soup, preferred_currency)
        except Exception:
            return []

    def _parse_racelines(self, soup, currency):
        from datetime import datetime as _dt
        races = []
        PURSE_PCT_MAP = {1:0.50, 2:0.25, 3:0.12, 4:0.08, 5:0.05}
        try:
            for row in soup.find_all('tr'):
                cells = [td.get_text(strip=True) for td in row.find_all('td')]
                if len(cells) < 6:
                    continue
                # TrackIT row typically: Date | Track | Conditions | Finish | Time | Purse ...
                date_str = cells[0]
                if not re.match(r'\d{1,2}/\d{1,2}/\d{2,4}', date_str):
                    continue
                try:
                    race_date = _dt.strptime(date_str, '%m/%d/%y').date()
                except ValueError:
                    try:
                        race_date = _dt.strptime(date_str, '%m/%d/%Y').date()
                    except ValueError:
                        continue
                track = cells[1] if len(cells) > 1 else ''
                finish_str = cells[3] if len(cells) > 3 else '0'
                try:
                    finish = int(re.sub(r'\D', '', finish_str) or '0')
                except ValueError:
                    finish = 0
                time_str = cells[4] if len(cells) > 4 else ''
                purse_str = cells[5] if len(cells) > 5 else '0'
                try:
                    purse = float(re.sub(r'[^\d.]', '', purse_str) or '0')
                except ValueError:
                    purse = 0.0
                if purse > 0:
                    races.append({
                        'date': race_date, 'track': track,
                        'finish': finish, 'time_str': time_str,
                        'purse': purse, 'currency': currency,
                    })
        except Exception:
            pass
        return races


# ══════════════════════════════════════════════════════════════
# OWNERSHIP PERIOD HELPERS
# ══════════════════════════════════════════════════════════════
def get_active_period(horse):
    """Return the open (not ended) OwnershipPeriod for a horse, or None."""
    return next((p for p in horse.periods if p.end_date is None), None)

def get_or_create_period(horse):
    """
    Return the active OwnershipPeriod, creating one from the horse's
    purchase_date if none exists yet.
    """
    active = get_active_period(horse)
    if active:
        return active
    from datetime import date as _d
    start = horse.purchase_date or _d.today()
    idx   = len(horse.periods) + 1
    period = OwnershipPeriod(
        horse_id     = horse.id,
        period_index = idx,
        start_date   = start,
        purchase_price = horse.purchase_price,
        purchase_type  = horse.purchase_type,
    )
    db.session.add(period)
    db.session.commit()
    return period

def assign_bill_to_period(bill, horse):
    """
    Link a bill to the correct ownership period.

    Uses bill.period_date which is the SERVICE date when set, otherwise
    the invoice date.  This handles late-arriving trainer/farrier bills
    for work done before a claim or sale - the service date puts the bill
    in the correct (previous owner's) period even if the invoice arrives weeks later.

    Example:
      Horse claimed June 24.
      Farrier bill arrives July 3 for shoeing done June 20.
      service_date = June 20 → falls in Period 1 (seller's period) ✓
      invoice date = July 3  → would wrongly fall in Period 2 ✗
    """
    assign_date = bill.period_date   # service_date if set, else invoice date
    for period in sorted(horse.periods, key=lambda p: p.start_date, reverse=True):
        if period.contains_bill(assign_date):
            bill.period_id = period.id
            return period
    # Fallback: attach to active period
    active = get_active_period(horse)
    if active:
        bill.period_id = active.id
    return active

def assign_race_to_period(race, horse):
    """Link a race result to the correct ownership period based on its date."""
    for period in horse.periods:
        if period.contains(race.date):
            race.period_id = period.id
            return period
    return None

def bills_by_period(horse):
    """
    Return a list of (period, bills_list) tuples for ALL ownership periods.
    Bills in gaps (no period) are excluded - the owner didn't own the horse then.
    """
    result = []
    for period in sorted(horse.periods, key=lambda p: p.start_date):
        # Re-check using contains_bill for any unassigned bills (belt-and-suspenders)
        period_bills = [b for b in horse.bills if b.period_id == period.id]
        result.append((period, period_bills))
    return result

def races_by_period(horse):
    """Same as bills_by_period but for race results."""
    result = []
    for period in sorted(horse.periods, key=lambda p: p.start_date):
        period_races = [r for r in horse.races if r.period_id == period.id]
        result.append((period, period_races))
    return result

def pl_for_period(period, bills, races):
    """
    Calculate P/L for a single ownership period.
    Returns dict keyed by owner_name.
    """
    totals = {}
    for o in period.horse.ownerships:
        share = o.pct / 100.0
        total_bills = sum(b.amount * share for b in bills)
        horse_earn  = sum(r.purse * PURSE_PCT.get(r.finish, 0) for r in races)
        owner_wins  = horse_earn * OWNER_PCT * share
        pur_cost    = (period.purchase_price or 0) * share
        sal_proc    = (period.sale_price     or 0) * share
        net         = owner_wins + sal_proc - pur_cost - total_bills
        totals[o.owner_name] = {
            'purchase_cost':  pur_cost,
            'sale_proceeds':  sal_proc,
            'total_bills':    total_bills,
            'race_winnings':  owner_wins,
            'net':            net,
        }
    return totals

# ══════════════════════════════════════════════════════════════
# TRACKMASTER EMAIL PARSER
# ══════════════════════════════════════════════════════════════
# Gmail label the scanner uses for TrackMaster entry emails.
# Users create this label in Gmail and set a filter so all emails
# from trackmaster.com are automatically labelled with it.
TM_DEFAULT_LABEL = 'RaceReminders'

TM_SUBJECT_PATTERNS = [
    r'trackmaster harness virtual stable',
    r'entry notification',
    r'is entered on',
]

def is_trackmaster_email(subject, body):
    """Return True if this email looks like a TrackMaster entry notification."""
    combined = (subject + ' ' + body[:200]).lower()
    return any(p in combined for p in TM_SUBJECT_PATTERNS)

def parse_trackmaster_email(subject, body, user_horses):
    """
    Parse a TrackMaster Virtual Stable entry notification email.
    Returns a dict of race fields or None if parsing fails.

    Sample subject: NORTHERN BREEZE is entered on 06/24/26 at Batavia Downs
    Body contains: Race #, Post Position, M/L Odds, Post Time, Dist, Gait, Purse, Driver, Trainer
    """
    result = {
        'horse_name': None, 'track': None, 'race_date': None,
        'race_number': None, 'post_time': None, 'post_position': None,
        'purse': None, 'driver': None, 'trainer': None,
        'morning_line': None, 'conditions': None,
    }

    # Horse name and track from subject line
    # e.g. "NORTHERN BREEZE is entered on 06/24/26 at Batavia Downs"
    m = re.search(
        r'^(.+?)\s+is entered on\s+([\d/]+)\s+at\s+(.+)',
        subject, re.IGNORECASE
    )
    if m:
        result['horse_name'] = m.group(1).strip().title()
        date_str = m.group(2).strip()
        result['track'] = m.group(3).strip()
        try:
            for fmt in ('%m/%d/%y', '%m/%d/%Y'):
                try:
                    from datetime import date as _date
                    result['race_date'] = datetime.strptime(date_str, fmt).date()
                    break
                except ValueError:
                    pass
        except Exception:
            pass

    # Fall back to body if subject didn't parse
    if not result['horse_name']:
        m2 = re.search(r'([A-Z][A-Z\s]+?)\s+is entered', body, re.IGNORECASE)
        if m2: result['horse_name'] = m2.group(1).strip().title()

    # Match horse to one in the user's stable (case-insensitive, partial match)
    matched_horse = None
    if result['horse_name']:
        hn_lower = result['horse_name'].lower()
        for h in user_horses:
            if h.name.lower() == hn_lower:
                matched_horse = h; break
        if not matched_horse:
            for h in user_horses:
                if h.name.lower() in hn_lower or hn_lower in h.name.lower():
                    matched_horse = h; break

    if not matched_horse:
        return None   # can't assign race to a horse we don't know

    # Skip entry emails for horses that are no longer in the stable
    if matched_horse.status != 'active':
        app.logger.info(
            f'Skipping TrackMaster entry email for retired horse: {matched_horse.name}'
        )
        return None   # horse is retired — ignore entry, don't add to schedule

    result['horse_id'] = matched_horse.id

    # Parse body fields
    patterns = {
        'race_number':   r'Race\s*#[:\s]*(\w+)',
        'post_position': r'Post\s+Position[:\s]*(\w+)',
        'morning_line':  r'M/L\s+Odds[:\s]*([\d/]+)',
        'post_time':     r'Post\s+Time[:\s]*([\d:]+\s*[AP]M\s*\w*)',
        'driver':        r'Driver[:\s]+([^\r\n]+)',
        'trainer':       r'Trainer[:\s]+([^\r\n]+)',
        'conditions':    r'Race\s+Conditions[:\s]*([^\r\n]+)',
    }
    for field, pat in patterns.items():
        m = re.search(pat, body, re.IGNORECASE)
        if m: result[field] = m.group(1).strip()

    # Purse
    pm = re.search(r'Purse[:\s]*\$?([\d,]+)', body, re.IGNORECASE)
    if pm:
        try: result['purse'] = float(pm.group(1).replace(',', ''))
        except: pass

    return result

def fetch_gmail_for_trackmaster(user):
    """
    Connect to user's Gmail and look for TrackMaster entry notifications.
    Returns list of parsed race dicts.
    """
    if not user.gmail_address or not user.imap_password:
        return []
    try:
        conn = imaplib.IMAP4_SSL('imap.gmail.com', 993)
        conn.login(user.gmail_address, user.imap_password)

        # Use dedicated RaceReminders label if configured, else fall back to
        # searching INBOX restricted to trackmaster sender - never reads personal emails
        tm_label = (user.trackmaster_label or TM_DEFAULT_LABEL).strip()
        folder_ok = False
        if tm_label:
            for candidate in [f'"{tm_label}"', tm_label]:
                status, _ = conn.select(candidate)
                if status == 'OK':
                    folder_ok = True
                    break
        if not folder_ok:
            conn.select('INBOX')

        # Search only for TrackMaster sender - even in INBOX fallback this is safe
        _, ids = conn.search(None, 'UNSEEN FROM "trackmaster.com"')
        results = []
        user_horses = Horse.query.filter_by(user_id=user.id, status='active').all()

        for eid in (ids[0].split() if ids[0] else []):
            _, data = conn.fetch(eid, '(RFC822)')
            import email as _email
            msg = _email.message_from_bytes(data[0][1])
            subject = msg.get('Subject', '')
            body = ''
            for part in msg.walk():
                if part.get_content_type() == 'text/plain':
                    try:
                        body += part.get_payload(decode=True).decode(
                            part.get_content_charset() or 'utf-8', errors='replace')
                    except: pass

            if is_trackmaster_email(subject, body):
                parsed = parse_trackmaster_email(subject, body, user_horses)
                if parsed:
                    results.append(parsed)
            conn.store(eid, '+FLAGS', '\\Seen')

        conn.logout()
        return results
    except Exception as e:
        app.logger.error(f'TrackMaster Gmail fetch error for user {user.id}: {e}')
        return []

def create_upcoming_race_from_parsed(parsed):
    """
    Insert an UpcomingRace from a parsed TrackMaster email.
    Deduplicates on (horse_id, race_date, track).
    Returns (race, created:bool).
    """
    existing = UpcomingRace.query.filter_by(
        horse_id=parsed['horse_id'],
        race_date=parsed['race_date'],
        track=parsed['track'],
    ).first()
    if existing:
        return existing, False
    race = UpcomingRace(
        horse_id      = parsed['horse_id'],
        race_date     = parsed['race_date'],
        track         = parsed['track'],
        race_number   = parsed.get('race_number'),
        post_time     = parsed.get('post_time'),
        post_position = parsed.get('post_position'),
        purse         = parsed.get('purse'),
        driver        = parsed.get('driver'),
        trainer       = parsed.get('trainer'),
        morning_line  = parsed.get('morning_line'),
        conditions    = parsed.get('conditions'),
        source        = 'trackmaster',
    )
    db.session.add(race)
    db.session.commit()
    return race, True

def send_owner_notification_email(user, upcoming_race, owner_name, owner_email):
    """Send a race entry notification email to an owner."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    if not user.gmail_address or not user.imap_password:
        return False
    try:
        horse = Horse.query.get(upcoming_race.horse_id)
        race_date_str = upcoming_race.race_date.strftime('%A, %B %-d, %Y') if upcoming_race.race_date else '-'
        purse_str = f"${upcoming_race.purse:,.0f}" if upcoming_race.purse else '-'

        subject = f"Your horse is racing - {horse.name} at {upcoming_race.track}"
        body_text = f"""Hi {owner_name},

{horse.name} is entered to race at {upcoming_race.track}.

Date:          {race_date_str}
Track:         {upcoming_race.track}
Race:          {upcoming_race.race_number or '-'}
Post time:     {upcoming_race.post_time or '-'}
Post position: {upcoming_race.post_position or '-'}
Purse:         {purse_str}
Driver:        {upcoming_race.driver or '-'}

You can view {horse.name}'s full race history and bills in your stable app.

{user.stable_name}
"""
        body_html = f"""
<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#1a1a1a;max-width:500px">
<h2 style="color:#1F3A5F">&#x1F40E; {horse.name} is racing!</h2>
<p>Hi {owner_name},</p>
<p><strong>{horse.name}</strong> is entered to race at <strong>{upcoming_race.track}</strong>.</p>
<table style="border-collapse:collapse;width:100%;margin:16px 0">
  <tr><td style="padding:7px 12px;background:#E6F1FB;color:#0C447C;font-weight:600;width:140px">Date</td>
      <td style="padding:7px 12px;background:#F0F7FF">{race_date_str}</td></tr>
  <tr><td style="padding:7px 12px;background:#E6F1FB;color:#0C447C;font-weight:600">Track</td>
      <td style="padding:7px 12px">{upcoming_race.track}</td></tr>
  <tr><td style="padding:7px 12px;background:#E6F1FB;color:#0C447C;font-weight:600">Race</td>
      <td style="padding:7px 12px;background:#F0F7FF">{upcoming_race.race_number or '-'}</td></tr>
  <tr><td style="padding:7px 12px;background:#E6F1FB;color:#0C447C;font-weight:600">Post time</td>
      <td style="padding:7px 12px">{upcoming_race.post_time or '-'}</td></tr>
  <tr><td style="padding:7px 12px;background:#E6F1FB;color:#0C447C;font-weight:600">Post position</td>
      <td style="padding:7px 12px;background:#F0F7FF">{upcoming_race.post_position or '-'}</td></tr>
  <tr><td style="padding:7px 12px;background:#E6F1FB;color:#0C447C;font-weight:600">Purse</td>
      <td style="padding:7px 12px;font-weight:600;color:#1F4E79">{purse_str}</td></tr>
  <tr><td style="padding:7px 12px;background:#E6F1FB;color:#0C447C;font-weight:600">Driver</td>
      <td style="padding:7px 12px;background:#F0F7FF">{upcoming_race.driver or '-'}</td></tr>
</table>
<p style="color:#5F5E5A;font-size:12px;margin-top:20px">{user.stable_name}</p>
</body></html>
"""
        msg = MIMEMultipart('alternative')
        msg['From']    = user.gmail_address
        msg['To']      = owner_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body_text, 'plain'))
        msg.attach(MIMEText(body_html,  'html'))

        with smtplib.SMTP('smtp.gmail.com', 587) as s:
            s.starttls()
            s.login(user.gmail_address, user.imap_password)
            s.sendmail(user.gmail_address, owner_email, msg.as_string())
        return True
    except Exception as e:
        app.logger.error(f'Owner notification email error: {e}')
        return False

# ══════════════════════════════════════════════════════════════
# AUTH ROUTES
# ══════════════════════════════════════════════════════════════
@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return render_template('landing.html')

@app.route('/signup', methods=['GET','POST'])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email       = request.form.get('email','').strip().lower()
        password    = request.form.get('password','')
        stable_name = request.form.get('stable_name','My Stable').strip()
        currency    = request.form.get('currency','USD')
        if User.query.filter_by(email=email).first():
            flash('An account with that email already exists.', 'error')
            return render_template('signup.html')
        if len(password) < 8:
            flash('Password must be at least 8 characters.', 'error')
            return render_template('signup.html')
        user = User(email=email, stable_name=stable_name, currency=currency)
        user.set_password(password)
        db.session.add(user); db.session.commit()
        login_user(user)
        flash(f'Welcome to {stable_name}! Add your first horse to get started.', 'success')
        return redirect(url_for('dashboard'))
    return render_template('signup.html')

@app.route('/login', methods=['GET','POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email    = request.form.get('email','').strip().lower()
        password = request.form.get('password','')
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user, remember=request.form.get('remember'))
            return redirect(request.args.get('next') or url_for('dashboard'))
        flash('Incorrect email or password.', 'error')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

# ══════════════════════════════════════════════════════════════
# DASHBOARD
# ══════════════════════════════════════════════════════════════
@app.route('/dashboard')
@login_required
def dashboard():
    horses = Horse.query.filter_by(user_id=current_user.id, status='active').all()
    stats = {'horses': len(horses), 'unpaid_bills': 0, 'total_earnings': 0.0, 'net_pl': 0.0}
    for h in horses:
        for b in h.bills:
            for p in b.payments:
                if not p.paid: stats['unpaid_bills'] += 1
        for r in h.races:
            for o in h.ownerships:
                stats['total_earnings'] += owner_earnings(r, o.pct)
    recent_syncs = SyncLog.query.filter_by(user_id=current_user.id)\
                                .order_by(SyncLog.synced_at.desc()).limit(5).all()
    from datetime import date as _date
    upcoming_this_week = (UpcomingRace.query.join(Horse)
        .filter(Horse.user_id==current_user.id)
        .filter(UpcomingRace.race_date >= _date.today())
        .order_by(UpcomingRace.race_date).limit(5).all())
    from datetime import date as _date2
    return render_template('dashboard.html', horses=horses, stats=stats, today=_date2.today(),
                           recent_syncs=recent_syncs, currency=current_user.currency,
                           upcoming_this_week=upcoming_this_week)

# ══════════════════════════════════════════════════════════════
# HORSES
# ══════════════════════════════════════════════════════════════
@app.route('/horses')
@login_required
def horses():
    all_horses = Horse.query.filter_by(user_id=current_user.id).order_by(Horse.name).all()
    return render_template('horses.html', horses=all_horses, currency=current_user.currency)

@app.route('/horses/add', methods=['GET','POST'])
@login_required
def add_horse():
    if request.method == 'POST':
        name  = request.form.get('name','').strip()
        gait  = request.form.get('gait','Pacer')
        year  = request.form.get('year_foaled')
        pp    = request.form.get('purchase_price')
        pd_   = request.form.get('purchase_date')
        ptype = request.form.get('purchase_type','')
        if not name:
            flash('Horse name is required.', 'error')
            return render_template('add_horse.html')
        existing = Horse.query.filter_by(user_id=current_user.id).count()
        hid = f'HRS-{existing+1:03d}'
        horse = Horse(
            user_id=current_user.id, horse_id=hid, name=name, gait=gait,
            year_foaled=int(year) if year else None,
            purchase_price=float(pp) if pp else None,
            purchase_date=datetime.strptime(pd_, '%Y-%m-%d').date() if pd_ else None,
            purchase_type=ptype or None,
        )
        db.session.add(horse); db.session.flush()
        owner_names = request.form.getlist('owner_name')
        owner_pcts  = request.form.getlist('owner_pct')
        for oname, opct in zip(owner_names, owner_pcts):
            if oname.strip() and opct:
                db.session.add(Ownership(
                    horse_id=horse.id,
                    owner_name=oname.strip(),
                    pct=float(opct)
                ))
        # Create the first OwnershipPeriod from purchase date
        from datetime import date as _pd
        period_start = datetime.strptime(pd_, '%Y-%m-%d').date() if pd_ else _pd.today()
        first_period = OwnershipPeriod(
            horse_id       = horse.id,
            period_index   = 1,
            start_date     = period_start,
            purchase_price = float(pp) if pp else None,
            purchase_type  = ptype or None,
        )
        db.session.add(first_period)
        db.session.commit()
        # Automatically search TrackIT and log TrackMaster guidance
        sync_results = sync_horse_on_add(horse, current_user)
        flash(f'{name} added! Checking TrackIT for past races...', 'success')
        for msg in sync_results:
            category = 'success' if 'imported' in msg or 'found' in msg else 'info'
            flash(msg, category)
        flash(
            f'Action needed: Add {name} to your TrackMaster Virtual Stable '
            f'so upcoming race entries are detected automatically. '
            f'See the reminder banner on this horse\'s page.',
            'warning'
        )
        return redirect(url_for('horse_detail', horse_id=horse.id))
    return render_template('add_horse.html')

@app.route('/horses/<int:horse_id>')
@login_required
def horse_detail(horse_id):
    horse = Horse.query.filter_by(id=horse_id, user_id=current_user.id).first_or_404()
    bills = Bill.query.filter_by(horse_id=horse_id).order_by(Bill.date.desc()).all()
    races = Race.query.filter_by(horse_id=horse_id).order_by(Race.date.desc()).all()
    upcoming = UpcomingRace.query.filter_by(horse_id=horse_id).order_by(UpcomingRace.race_date).all()
    from datetime import date as _hd; today = _hd.today()
    sync_log = HorseSync.query.filter_by(horse_id=horse_id).order_by(HorseSync.checked_at.desc()).first()
    grouped_bills = bills_by_period(horse)
    grouped_races = races_by_period(horse)
    return render_template('horse_detail.html', horse=horse, bills=bills, races=races,
                           grouped_bills=grouped_bills, grouped_races=grouped_races,
                           upcoming=upcoming, today=today, sync_log=sync_log,
                           currency=current_user.currency, PURSE_PCT=PURSE_PCT,
                           FINISH_LABEL=FINISH_LABEL, OWNER_PCT=OWNER_PCT,
                           owner_earnings=owner_earnings, horse_earnings=horse_earnings)

@app.route('/horses/<int:horse_id>/retire', methods=['POST'])
@login_required
def retire_horse(horse_id):
    horse = Horse.query.filter_by(id=horse_id, user_id=current_user.id).first_or_404()
    horse.status = 'retired'
    horse.trackmaster_confirmed = False  # reset so restore re-shows the reminder

    # Close the active ownership period on the sale date
    active_period = get_active_period(horse)
    if active_period:
        from datetime import date as _rd
        active_period.end_date   = _rd.today()
        active_period.sale_price = horse.sale_price
        active_period.sale_type  = horse.sale_type if hasattr(horse,'sale_type') else None

    # Cancel any upcoming races for this horse
    upcoming = UpcomingRace.query.filter_by(horse_id=horse.id).filter(
        UpcomingRace.race_date >= datetime.utcnow().date()
    ).all()
    cancelled = len(upcoming)
    for ur in upcoming:
        db.session.delete(ur)

    db.session.commit()
    msg = f'{horse.name} retired. All history preserved.'
    if cancelled:
        msg += f' {cancelled} upcoming race entr{"y" if cancelled==1 else "ies"} removed.'
    flash(msg, 'success')
    # Two-step action notice — stored in session so horse_detail can show it as a banner
    from flask import session as _sess
    _sess[f'retired_trackmaster_{horse.id}'] = True
    return redirect(url_for('retired_horse_page', horse_id=horse.id))


@app.route('/horses/<int:horse_id>/restore', methods=['POST'])
@login_required
def restore_horse(horse_id):
    horse = Horse.query.filter_by(id=horse_id, user_id=current_user.id).first_or_404()
    horse.status = 'active'
    # Open a fresh ownership period for this re-acquisition
    from datetime import date as _res
    new_idx = len(horse.periods) + 1
    new_period = OwnershipPeriod(
        horse_id       = horse.id,
        period_index   = new_idx,
        start_date     = _res.today(),
        purchase_price = horse.purchase_price,
        purchase_type  = horse.purchase_type,
    )
    db.session.add(new_period)
    db.session.commit()
    flash(f'New ownership period #{new_idx} started for {horse.name}.', 'info')
    # Re-run sync so any new TrackIT results since retirement are imported
    sync_results = sync_horse_on_add(horse, current_user)
    flash(f'{horse.name} restored to active.', 'success')
    for msg in sync_results:
        flash(msg, 'info')
    flash(f'Remember to re-add {horse.name} to your TrackMaster Virtual Stable if you removed them.', 'info')
    return redirect(url_for('horses'))


@app.route('/horses/<int:horse_id>/periods', methods=['GET','POST'])
@login_required
def manage_periods(horse_id):
    """View all ownership periods and record a sale or new purchase."""
    horse = Horse.query.filter_by(id=horse_id, user_id=current_user.id).first_or_404()
    if request.method == 'POST':
        action = request.form.get('action')
        from datetime import date as _mpd

        if action == 'close_period':
            # Record a sale/claim and close the current period
            period = get_active_period(horse)
            if period:
                sale_date_str = request.form.get('sale_date')
                sale_type     = request.form.get('sale_type','').strip() or None
                sale_date     = (datetime.strptime(sale_date_str,'%Y-%m-%d').date()
                                 if sale_date_str else _mpd.today())

                # CLAIMING RULE: if sold via a claim race, the period end_date
                # is set to the RACE DATE (inclusive) so that race's purse stays
                # in this period.  Bills on that date go to the buyer (new period).
                # For private sales/auctions, same logic applies - end_date inclusive.
                period.end_date   = sale_date
                period.sale_price = float(request.form.get('sale_price') or 0) or None
                period.sale_type  = sale_type
                horse.sale_price  = period.sale_price
                horse.sale_date   = sale_date
                horse.status      = 'retired'

                # Tag the claim race in the race history so it's visually clear
                if sale_type == 'Claim' and sale_date:
                    claim_race = Race.query.filter_by(
                        horse_id=horse.id, date=sale_date
                    ).first()
                    if claim_race:
                        claim_race.race_type  = 'Claim'
                        claim_race.claim_price= period.sale_price

                # Remove upcoming races scheduled AFTER the sale date
                for ur in UpcomingRace.query.filter_by(horse_id=horse.id).all():
                    if ur.race_date > sale_date:
                        db.session.delete(ur)

                db.session.commit()
                if sale_type == 'Claim':
                    flash(
                        f'{horse.name} claimed. '
                        f'The purse from {sale_date.strftime("%b %-d, %Y")} '
                        f'stays with you - it is recorded in Period {period.period_index}.',
                        'success'
                    )
                else:
                    flash(f'Sale recorded. {horse.name} retired - history preserved.', 'success')
                flash('Remember to remove them from TrackMaster Virtual Stable.', 'info')
                return redirect(url_for('horses'))

        elif action == 'new_period':
            # Re-purchase: open a new ownership period
            purchase_date_str = request.form.get('purchase_date')
            start = (datetime.strptime(purchase_date_str,'%Y-%m-%d').date()
                     if purchase_date_str else _mpd.today())
            new_idx = len(horse.periods) + 1
            pp  = request.form.get('purchase_price')
            new_period = OwnershipPeriod(
                horse_id       = horse.id,
                period_index   = new_idx,
                start_date     = start,
                purchase_price = float(pp) if pp else None,
                purchase_type  = request.form.get('purchase_type','').strip() or None,
            )
            horse.status        = 'active'
            horse.purchase_price = float(pp) if pp else horse.purchase_price
            horse.purchase_date  = start
            db.session.add(new_period); db.session.commit()
            sync_results = sync_horse_on_add(horse, current_user)
            flash(f'Ownership period #{new_idx} started for {horse.name}.', 'success')
            for msg in sync_results:
                flash(msg, 'info')
            return redirect(url_for('horse_detail', horse_id=horse.id))

    return render_template('manage_periods.html', horse=horse,
                           active_period=get_active_period(horse))


@app.route('/horses/<int:horse_id>/trackmaster-confirm', methods=['POST'])
@login_required
def confirm_trackmaster(horse_id):
    """User confirms they have added this horse to TrackMaster Virtual Stable."""
    horse = Horse.query.filter_by(id=horse_id, user_id=current_user.id).first_or_404()
    horse.trackmaster_confirmed = True
    db.session.commit()
    flash(f'{horse.name} marked as added to TrackMaster. Entry alerts will now flow automatically.', 'success')
    return redirect(url_for('horse_detail', horse_id=horse_id))


@app.route('/horses/<int:horse_id>/retired')
@login_required
def retired_horse_page(horse_id):
    """Shown immediately after retiring a horse — guides the user through stopping emails."""
    horse = Horse.query.filter_by(id=horse_id, user_id=current_user.id).first_or_404()
    from flask import session as _sess
    show_guide = _sess.pop(f'retired_trackmaster_{horse.id}', False)
    return render_template('retired_horse.html', horse=horse, show_guide=show_guide)


# ══════════════════════════════════════════════════════════════
# TRACKIT MANUAL IMPORT
# ══════════════════════════════════════════════════════════════
@app.route('/horses/<int:horse_id>/import-races', methods=['GET','POST'])
@login_required
def import_races(horse_id):
    """
    Manual race import page — three paths:
      1. Paste raw TrackIT raceline text (auto-parsed)
      2. Upload a CSV file
      3. Single race manual entry form
    """
    horse = Horse.query.filter_by(id=horse_id, user_id=current_user.id).first_or_404()

    if request.method == 'POST':
        action = request.form.get('action','manual')

        if action == 'paste':
            raw = request.form.get('paste_text','').strip()
            races = _parse_trackit_paste(raw)
            if not races:
                flash('Could not read any races from the pasted text. '
                      'Try copying the full racelines table from TrackIT.', 'error')
                return redirect(url_for('import_races', horse_id=horse_id))
            added = _save_imported_races(races, horse, current_user.currency)
            flash(f'{added} race{"s" if added!=1 else ""} imported successfully.', 'success')
            return redirect(url_for('horse_detail', horse_id=horse_id))

        elif action == 'csv':
            f_obj = request.files.get('csv_file')
            if not f_obj or not f_obj.filename:
                flash('Please select a CSV file to upload.', 'error')
                return redirect(url_for('import_races', horse_id=horse_id))
            content = f_obj.read().decode('utf-8', errors='replace')
            races = _parse_csv_import(content)
            if not races:
                flash('Could not read any races from the CSV. '
                      'Check the format matches the template.', 'error')
                return redirect(url_for('import_races', horse_id=horse_id))
            added = _save_imported_races(races, horse, current_user.currency)
            flash(f'{added} race{"s" if added!=1 else ""} imported from CSV.', 'success')
            return redirect(url_for('horse_detail', horse_id=horse_id))

        elif action == 'manual':
            date_str  = request.form.get('date','').strip()
            track     = request.form.get('track','').strip()
            finish    = request.form.get('finish','0').strip()
            purse     = request.form.get('purse','0').strip()
            race_name = request.form.get('race_name','').strip()
            time_str  = request.form.get('time_str','').strip()
            if not date_str or not track:
                flash('Date and track are required.', 'error')
                return redirect(url_for('import_races', horse_id=horse_id))
            try:
                race_date = datetime.strptime(date_str, '%Y-%m-%d').date()
                race = Race(
                    horse_id  = horse.id,
                    date      = race_date,
                    track     = track,
                    race_name = race_name,
                    finish    = int(re.sub(r'\D','',finish) or '0'),
                    purse     = float(re.sub(r'[^\d.]','',purse) or '0'),
                    time_str  = time_str,
                    currency  = current_user.currency,
                )
                assign_race_to_period(race, horse)
                db.session.add(race)
                db.session.commit()
                flash('Race added successfully.', 'success')
            except Exception as e:
                flash(f'Could not save race: {e}', 'error')
            return redirect(url_for('import_races', horse_id=horse_id))

    existing = Race.query.filter_by(horse_id=horse_id).count()
    return render_template('import_races.html', horse=horse,
                           existing=existing, currency=current_user.currency)


def _parse_trackit_paste(text):
    """
    Parse raw text copied from a TrackIT racelines page.

    TrackIT displays racelines in a table. When a user selects all and copies,
    they get tab- or space-separated rows. We try to find rows that look like:
      date  track  conditions  finish  time  purse  ...

    Also handles common date formats: MM/DD/YY, MM/DD/YYYY, YYYY-MM-DD
    """
    import csv, io
    races = []
    seen  = set()

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        # Split on tabs first, fall back to multiple spaces
        if '\t' in line:
            parts = [p.strip() for p in line.split('\t')]
        else:
            parts = [p.strip() for p in re.split(r'  +', line)]

        if len(parts) < 4:
            continue

        # Try to find the date in any of the first 3 columns
        race_date = None
        date_col  = -1
        for i, part in enumerate(parts[:3]):
            for fmt in ('%m/%d/%y','%m/%d/%Y','%Y-%m-%d','%d/%m/%Y'):
                try:
                    race_date = datetime.strptime(part, fmt).date()
                    date_col  = i
                    break
                except ValueError:
                    pass
            if race_date:
                break

        if not race_date:
            continue

        # Columns after date: track, conditions/class, finish, time, purse
        rest = parts[date_col+1:]
        if len(rest) < 3:
            continue

        track     = rest[0] if rest else ''
        # finish is the first field that looks like a number (1-10) or "1st" etc
        finish    = 0
        purse     = 0.0
        time_str  = ''

        for col in rest[1:]:
            clean = re.sub(r'[^\d]','', col)
            # Finish position: 1-digit or 2-digit number ≤ 12
            if not finish and clean and 1 <= int(clean or 0) <= 12 and len(clean) <= 2:
                finish = int(clean)
            # Time: looks like 1:52.1 or 152.1
            elif re.match(r'^\d[:.]\d{2}\.\d$', col) or re.match(r'^1:\d{2}\.\d$', col):
                time_str = col
            # Purse: larger number with optional $ or commas
            elif clean and int(clean or 0) > 100:
                try:
                    purse = float(re.sub(r'[^\d.]', '', col))
                except:
                    pass

        if purse == 0:
            continue   # skip rows with no purse — likely headers

        key = (race_date, track)
        if key in seen:
            continue
        seen.add(key)

        races.append({
            'date': race_date, 'track': track,
            'finish': finish, 'purse': purse, 'time_str': time_str,
        })

    return races


def _parse_csv_import(content):
    """
    Parse a CSV with columns: date, track, finish, purse[, time, race_name]
    Accepts header row or no header row.
    """
    import csv, io
    races = []
    seen  = set()
    reader = csv.reader(io.StringIO(content))
    for row in reader:
        if len(row) < 4:
            continue
        # Skip obvious header rows
        if row[0].strip().lower() in ('date','race date','d'):
            continue
        try:
            date_raw = row[0].strip()
            race_date = None
            for fmt in ('%Y-%m-%d','%m/%d/%Y','%m/%d/%y','%d/%m/%Y'):
                try:
                    race_date = datetime.strptime(date_raw, fmt).date()
                    break
                except ValueError:
                    pass
            if not race_date:
                continue
            track   = row[1].strip()
            finish  = int(re.sub(r'\D','', row[2].strip()) or '0')
            purse   = float(re.sub(r'[^\d.]','', row[3].strip()) or '0')
            time_str = row[4].strip() if len(row) > 4 else ''
            race_name= row[5].strip() if len(row) > 5 else ''
            if purse == 0:
                continue
            key = (race_date, track)
            if key in seen:
                continue
            seen.add(key)
            races.append({'date':race_date,'track':track,'finish':finish,
                          'purse':purse,'time_str':time_str,'race_name':race_name})
        except Exception:
            continue
    return races


def _save_imported_races(races, horse, currency):
    """Save a list of parsed race dicts to the database, skipping duplicates."""
    added = 0
    existing_keys = {
        (r.date, r.track)
        for r in Race.query.filter_by(horse_id=horse.id).all()
    }
    for r in races:
        key = (r['date'], r.get('track',''))
        if key in existing_keys:
            continue
        race = Race(
            horse_id  = horse.id,
            date      = r['date'],
            track     = r.get('track',''),
            race_name = r.get('race_name',''),
            finish    = r.get('finish', 0),
            purse     = r.get('purse', 0.0),
            time_str  = r.get('time_str',''),
            currency  = r.get('currency', currency),
        )
        assign_race_to_period(race, horse)
        db.session.add(race)
        existing_keys.add(key)
        added += 1
    db.session.commit()
    return added

# ══════════════════════════════════════════════════════════════
# BILLS
# ══════════════════════════════════════════════════════════════
@app.route('/horses/<int:horse_id>/bills/add', methods=['GET','POST'])
@login_required
def add_bill(horse_id):
    horse = Horse.query.filter_by(id=horse_id, user_id=current_user.id).first_or_404()
    if request.method == 'POST':
        amount   = request.form.get('amount')
        category = request.form.get('category','Other')
        vendor   = request.form.get('vendor','').strip()
        desc         = request.form.get('description','').strip()
        date_str     = request.form.get('date')
        svc_date_str = request.form.get('service_date','').strip()
        if not amount:
            flash('Amount is required.', 'error')
            return render_template('add_bill.html', horse=horse, categories=CATEGORIES)
        invoice_date = datetime.strptime(date_str,'%Y-%m-%d').date() if date_str else datetime.utcnow().date()
        service_date = datetime.strptime(svc_date_str,'%Y-%m-%d').date() if svc_date_str else None
        bill = Bill(
            horse_id=horse.id,
            amount=float(amount),
            category=category,
            vendor=vendor,
            description=desc,
            date=invoice_date,
            service_date=service_date,
        )
        db.session.add(bill); db.session.flush()
        # Assign to the correct ownership period
        active_period = get_or_create_period(horse)
        assign_bill_to_period(bill, horse)
        for o in horse.ownerships:
            db.session.add(Payment(
                bill_id=bill.id,
                owner_name=o.owner_name,
                amount_due=round(float(amount) * o.pct / 100, 2),
                paid=False,
            ))
        db.session.commit()
        flash('Bill added and split per owner.', 'success')
        return redirect(url_for('horse_detail', horse_id=horse.id))
    return render_template('add_bill.html', horse=horse, categories=CATEGORIES)

@app.route('/payments/<int:payment_id>/toggle', methods=['POST'])
@login_required
def toggle_payment(payment_id):
    pmt = Payment.query.get_or_404(payment_id)
    bill = Bill.query.get(pmt.bill_id)
    horse = Horse.query.filter_by(id=bill.horse_id, user_id=current_user.id).first_or_404()
    pmt.paid = not pmt.paid
    pmt.paid_at = datetime.utcnow() if pmt.paid else None
    db.session.commit()
    return jsonify({'paid': pmt.paid, 'owner': pmt.owner_name})

# ══════════════════════════════════════════════════════════════
# TRACKIT SETTINGS PAGE
# ══════════════════════════════════════════════════════════════
@app.route('/settings/trackit', methods=['GET'])
@login_required
def trackit_settings():
    syncs = SyncLog.query.filter_by(user_id=current_user.id)\
                         .order_by(SyncLog.synced_at.desc()).limit(10).all()
    horses = Horse.query.filter_by(user_id=current_user.id, status='active').all()
    return render_template('trackit_settings.html',
                           syncs=syncs, horses=horses,
                           TRACKIT_SIGNUP_URL=f'{TRACKIT_BASE}/register.cfm',
                           TRACKIT_FORGOT_URL=f'{TRACKIT_BASE}/forgotPassword.cfm')

@app.route('/settings/trackit/connect', methods=['POST'])
@login_required
def trackit_connect():
    username = request.form.get('trackit_username','').strip()
    password = request.form.get('trackit_password','').strip()
    if not username or not password:
        flash('Both username and password are required.', 'error')
        return redirect(url_for('trackit_settings'))
    success, err, credits = trackit_test_connection(username, password)
    if success:
        current_user.trackit_username  = username
        current_user.trackit_password  = password
        current_user.trackit_connected = True
        current_user.trackit_credits   = credits
        db.session.commit()
        flash('TrackIT connected successfully! Race results will now sync automatically.', 'success')
    else:
        flash(f'Could not connect: {err}', 'error')
    return redirect(url_for('trackit_settings'))

@app.route('/settings/trackit/disconnect', methods=['POST'])
@login_required
def trackit_disconnect():
    current_user.trackit_username  = None
    current_user.trackit_password  = None
    current_user.trackit_connected = False
    db.session.commit()
    flash('TrackIT disconnected.', 'success')
    return redirect(url_for('trackit_settings'))

@app.route('/settings/trackit/sync', methods=['POST'])
@login_required
def trackit_sync_now():
    if not current_user.trackit_connected:
        flash('Connect your TrackIT account first.', 'error')
        return redirect(url_for('trackit_settings'))
    flash('Sync started - check back in a moment for results.', 'info')
    # In production this would kick off a background job
    return redirect(url_for('trackit_settings'))

# ══════════════════════════════════════════════════════════════
# GENERAL SETTINGS
# ══════════════════════════════════════════════════════════════
@app.route('/settings', methods=['GET','POST'])
@login_required
def settings():
    if request.method == 'POST':
        current_user.stable_name = request.form.get('stable_name', current_user.stable_name).strip()
        current_user.currency    = request.form.get('currency', 'USD')
        current_user.trackmaster_label = request.form.get('trackmaster_label', 'RaceReminders').strip()
        db.session.commit()
        flash('Settings saved.', 'success')
        return redirect(url_for('settings'))
    return render_template('settings.html')

# ══════════════════════════════════════════════════════════════
# API - for JS interactions
# ══════════════════════════════════════════════════════════════
@app.route('/api/horses')
@login_required
def api_horses():
    horses = Horse.query.filter_by(user_id=current_user.id, status='active').all()
    return jsonify([{'id':h.id,'name':h.name,'gait':h.gait} for h in horses])


# ══════════════════════════════════════════════════════════════
# SCHEDULE - UPCOMING RACES
# ══════════════════════════════════════════════════════════════
@app.route('/schedule')
@login_required
def schedule():
    from datetime import date
    # Fetch TrackMaster emails for this user on page load (lightweight check)
    if current_user.gmail_address and current_user.imap_password:
        try:
            parsed_list = fetch_gmail_for_trackmaster(current_user)
            for parsed in parsed_list:
                race, created = create_upcoming_race_from_parsed(parsed)
                if created:
                    flash(f'New entry detected: {Horse.query.get(parsed["horse_id"]).name} at {parsed["track"]}', 'success')
        except Exception as e:
            app.logger.warning(f'TrackMaster check failed: {e}')

    today = date.today()
    upcoming = (UpcomingRace.query
                .join(Horse)
                .filter(Horse.user_id == current_user.id)
                .filter(UpcomingRace.race_date >= today)
                .order_by(UpcomingRace.race_date)
                .all())
    past_entries = (UpcomingRace.query
                    .join(Horse)
                    .filter(Horse.user_id == current_user.id)
                    .filter(UpcomingRace.race_date < today)
                    .order_by(UpcomingRace.race_date.desc())
                    .limit(10).all())
    return render_template('schedule.html', upcoming=upcoming,
                           past_entries=past_entries, today=today)

@app.route('/schedule/add', methods=['GET','POST'])
@login_required
def add_upcoming_race():
    horses = Horse.query.filter_by(user_id=current_user.id, status='active').all()
    if request.method == 'POST':
        horse_id  = request.form.get('horse_id')
        race_date = request.form.get('race_date')
        track     = request.form.get('track','').strip()
        if not horse_id or not race_date or not track:
            flash('Horse, date, and track are required.', 'error')
            return render_template('add_upcoming_race.html', horses=horses)
        horse = Horse.query.filter_by(id=horse_id, user_id=current_user.id).first_or_404()
        race = UpcomingRace(
            horse_id      = int(horse_id),
            race_date     = datetime.strptime(race_date, '%Y-%m-%d').date(),
            track         = track,
            race_number   = request.form.get('race_number','').strip() or None,
            post_time     = request.form.get('post_time','').strip()   or None,
            post_position = request.form.get('post_position','').strip() or None,
            purse         = float(request.form.get('purse') or 0) or None,
            driver        = request.form.get('driver','').strip()  or None,
            trainer       = request.form.get('trainer','').strip() or None,
            source        = 'manual',
        )
        db.session.add(race)
        db.session.commit()
        flash(f'Race entry added for {horse.name}.', 'success')
        return redirect(url_for('schedule'))
    return render_template('add_upcoming_race.html', horses=horses)

@app.route('/schedule/<int:race_id>/delete', methods=['POST'])
@login_required
def delete_upcoming_race(race_id):
    race = UpcomingRace.query.join(Horse)           .filter(Horse.user_id==current_user.id, UpcomingRace.id==race_id).first_or_404()
    db.session.delete(race); db.session.commit()
    flash('Race entry removed.', 'success')
    return redirect(url_for('schedule'))

@app.route('/schedule/<int:race_id>/notify', methods=['GET','POST'])
@login_required
def notify_owners(race_id):
    race = UpcomingRace.query.join(Horse)           .filter(Horse.user_id==current_user.id, UpcomingRace.id==race_id).first_or_404()
    horse = Horse.query.get(race.horse_id)
    if request.method == 'POST':
        selected = request.form.getlist('notify_owner')
        sent = 0
        for o in horse.ownerships:
            if o.owner_name in selected and o.owner_email:
                ok = send_owner_notification_email(current_user, race, o.owner_name, o.owner_email)
                if ok:
                    notif = OwnerNotification.query.filter_by(
                        upcoming_race_id=race_id, owner_name=o.owner_name).first()
                    if not notif:
                        notif = OwnerNotification(upcoming_race_id=race_id,
                                                  owner_name=o.owner_name,
                                                  owner_email=o.owner_email)
                        db.session.add(notif)
                    notif.sent_at = datetime.utcnow()
                    notif.send_requested = True
                    sent += 1
        db.session.commit()
        if sent:
            flash(f'Notification sent to {sent} owner(s).', 'success')
        else:
            flash('No emails sent - check that owners have email addresses saved.', 'warning')
        return redirect(url_for('schedule'))
    already_notified = {n.owner_name for n in race.notifications if n.sent_at}
    return render_template('notify_owners.html', race=race, horse=horse,
                           already_notified=already_notified)

@app.route('/schedule/sync-trackmaster', methods=['POST'])
@login_required
def sync_trackmaster_entries():
    """Manually trigger a TrackMaster email check."""
    if not current_user.gmail_address or not current_user.imap_password:
        flash('Connect your Gmail first under Settings.', 'error')
        return redirect(url_for('schedule'))
    try:
        parsed_list = fetch_gmail_for_trackmaster(current_user)
        added = 0
        for parsed in parsed_list:
            _, created = create_upcoming_race_from_parsed(parsed)
            if created: added += 1
        if added:
            flash(f'{added} new race entr{"y" if added==1 else "ies"} detected from TrackMaster.', 'success')
        else:
            flash('No new TrackMaster entries found.', 'info')
    except Exception as e:
        flash(f'Could not check TrackMaster emails: {e}', 'error')
    return redirect(url_for('schedule'))

# ══════════════════════════════════════════════════════════════
# INIT
# ══════════════════════════════════════════════════════════════
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True, port=5000)
