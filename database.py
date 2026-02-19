#!/usr/bin/env python3
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from werkzeug.security import generate_password_hash, check_password_hash
import os
from datetime import datetime, timedelta

# ─── PASSWORD HELPERS ──────────────────────────────────────────────────────────

def hash_password(plain_text):
    """Hash a plain text password using werkzeug pbkdf2."""
    return generate_password_hash(plain_text)

def check_pw(stored, provided):
    """
    Returns True if the provided password matches the stored one.
    Handles BOTH hashed passwords (new) and plain-text passwords (legacy accounts).
    """
    try:
        return check_password_hash(stored, provided)
    except Exception:
        # stored is plain text — legacy account
        return stored == provided

def is_hashed(stored):
    """Returns True if the password is already hashed (not plain text)."""
    return stored.startswith('pbkdf2:') or stored.startswith('scrypt:')

# ─── XP / LEVEL FORMULA ─────────────────────────────────────────────────────
def xp_threshold_for_level(level):
    if level <= 1:
        return 0
    total = 0
    for l in range(1, level):
        total += 25 * (2 ** (l - 1))
    return total

def calculate_level_from_xp(xp):
    level = 1
    while True:
        next_threshold = xp_threshold_for_level(level + 1)
        if xp >= next_threshold:
            level += 1
        else:
            break
    return level

# ─── DB CONNECTION ─────────────────────────────────────────────────────────────
MONGO_URI = os.getenv('MONGODB_URI') or os.getenv('MONGO_URI')

if not MONGO_URI:
    print("WARNING: No MongoDB URI found! Data will NOT persist.")
    MONGO_URI = 'mongodb://localhost:27017/'

try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    client.admin.command('ping')
    db = client['gayathri_smart_speak']
    users_collection = db['users']
    teachers_collection = db['teachers']
    conversations_collection = db['conversations']
    admins_collection = db['admins']
    print("MongoDB connected successfully!")
except Exception as e:
    print(f"MongoDB connection failed: {e}")
    client = None
    db = None
    users_collection = None
    teachers_collection = None
    conversations_collection = None
    admins_collection = None

# ─── BADGES ──────────────────────────────────────────────────────────────────
ALL_BADGES = {
    'level_2':    {'name':'Level 2 Reached!',    'icon':'🏅','desc':'Reach Level 2',                   'type':'level',                  'req':2},
    'level_5':    {'name':'Level 5 Star!',        'icon':'🌠','desc':'Reach Level 5',                   'type':'level',                  'req':5},
    'level_10':   {'name':'Level 10 Legend!',     'icon':'🏆','desc':'Reach Level 10',                  'type':'level',                  'req':10},
    'level_15':   {'name':'Level 15 Pro!',        'icon':'💎','desc':'Reach Level 15',                  'type':'level',                  'req':15},
    'xp_25':      {'name':'First 25 XP!',         'icon':'✨','desc':'Earn your first 25 XP',           'type':'total_xp',               'req':25},
    'xp_100':     {'name':'100 XP Club!',          'icon':'💯','desc':'Earn 100 XP',                    'type':'total_xp',               'req':100},
    'xp_500':     {'name':'500 XP Achiever!',      'icon':'🎯','desc':'Earn 500 XP',                    'type':'total_xp',               'req':500},
    'xp_1000':    {'name':'1000 XP Master!',       'icon':'🚀','desc':'Earn 1000 XP',                   'type':'total_xp',               'req':1000},
    'conv_5':     {'name':'First Chat!',           'icon':'💬','desc':'Have 5 English conversations',   'type':'conversation_count',     'req':5},
    'conv_25':    {'name':'Chatterbox!',           'icon':'🗣️','desc':'Have 25 conversations',          'type':'conversation_count',     'req':25},
    'roleplay_5': {'name':'Roleplay Beginner!',    'icon':'🎭','desc':'Complete 5 roleplay sessions',   'type':'roleplay_count',         'req':5},
    'roleplay_20':{'name':'Drama Star!',           'icon':'🎬','desc':'Complete 20 roleplay sessions',  'type':'roleplay_count',         'req':20},
    'repeat_10':  {'name':'First 10 Sentences!',  'icon':'🔁','desc':'Repeat 10 sentences correctly',  'type':'repeat_count',           'req':10},
    'repeat_50':  {'name':'Repeat Champ!',        'icon':'🎤','desc':'Repeat 50 sentences correctly',  'type':'repeat_count',           'req':50},
    'repeat_200': {'name':'Repeat Master!',       'icon':'🌟','desc':'Repeat 200 sentences correctly', 'type':'repeat_count',           'req':200},
    'spell_5':    {'name':'First 5 Words!',        'icon':'🐝','desc':'Spell 5 words correctly',        'type':'spelling_count',         'req':5},
    'spell_25':   {'name':'Speller!',              'icon':'📝','desc':'Spell 25 words correctly',       'type':'spelling_count',         'req':25},
    'spell_100':  {'name':'Spell Master!',         'icon':'📚','desc':'Spell 100 words correctly',      'type':'spelling_count',         'req':100},
    'vocab_10':   {'name':'Word Explorer!',        'icon':'📖','desc':'Look up 10 word meanings',       'type':'vocabulary_count',       'req':10},
    'vocab_50':   {'name':'Word Collector!',       'icon':'📚','desc':'Look up 50 word meanings',       'type':'vocabulary_count',       'req':50},
    'perfect_5':  {'name':'Pronunciation Star!',  'icon':'💫','desc':'Score 80%+ pronunciation 5 times','type':'high_pronunciation_count','req':5},
    'perfect_25': {'name':'Pronunciation Pro!',   'icon':'⭐','desc':'Score 80%+ pronunciation 25 times','type':'high_pronunciation_count','req':25},
}

def init_user_achievements():
    return {
        'badges_earned': [], 'daily_login_streak': 0, 'max_login_streak': 0,
        'last_login': None, 'conversation_count': 0, 'roleplay_count': 0,
        'repeat_count': 0, 'spelling_count': 0, 'vocabulary_count': 0,
        'high_pronunciation_count': 0, 'challenge_streak': 0
    }

def check_and_award_badges(user_id):
    if users_collection is None: return []
    try:
        user = users_collection.find_one({'_id': user_id})
        if not user: return []
        achievements = user.get('achievements', init_user_achievements())
        if not isinstance(achievements, dict):
            achievements = init_user_achievements()
            users_collection.update_one({'_id': user_id}, {'$set': {'achievements': achievements}})
        earned = achievements.get('badges_earned', [])
        if not isinstance(earned, list): earned = []
        new_badges = []
        for bid, badge in ALL_BADGES.items():
            if bid in earned: continue
            vt = badge['type']
            req = badge['req']
            if vt == 'level': current = user.get('level', 1)
            elif vt == 'total_xp': current = user.get('total_xp', 0)
            else: current = achievements.get(vt, 0) if isinstance(achievements, dict) else 0
            if isinstance(current, (int, float)) and current >= req: new_badges.append(bid)
        if new_badges:
            earned.extend(new_badges)
            users_collection.update_one({'_id': user_id}, {'$set': {'achievements.badges_earned': earned}})
        return new_badges
    except Exception as e:
        print(f"Badge check error: {e}"); return []

def update_login_streak(user_id):
    if users_collection is None: return
    try:
        user = users_collection.find_one({'_id': user_id})
        if not user: return
        achievements = user.get('achievements', init_user_achievements())
        if not isinstance(achievements, dict):
            achievements = init_user_achievements()
            users_collection.update_one({'_id': user_id}, {'$set': {'achievements': achievements}})
        today = datetime.now().strftime('%Y-%m-%d')
        last_login = achievements.get('last_login')
        if last_login == today: return
        streak = achievements.get('daily_login_streak', 0)
        yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        streak = streak + 1 if last_login == yesterday else 1
        max_streak = max(streak, achievements.get('max_login_streak', 0))
        users_collection.update_one({'_id': user_id}, {'$set': {
            'achievements.daily_login_streak': streak,
            'achievements.max_login_streak': max_streak,
            'achievements.last_login': today
        }})
        check_and_award_badges(user_id)
    except Exception as e:
        print(f"Login streak error: {e}")

def increment_activity(user_id, activity_type, amount=1):
    if users_collection is None: return
    try:
        field = f'achievements.{activity_type}_count'
        users_collection.update_one({'_id': user_id}, {'$inc': {field: amount}})
        check_and_award_badges(user_id)
    except Exception as e:
        print(f"Activity error: {e}")

def log_mistake(user_id, mistake_type, mistake_data):
    if users_collection is None: return
    try:
        user = users_collection.find_one({'_id': user_id})
        if not user: return
        mistakes = user.get('mistakes', {'pronunciation':[],'spelling':[],'vocabulary':[],'total':0})
        entry = {'data': mistake_data, 'time': datetime.now().strftime('%Y-%m-%d %H:%M')}
        if mistake_type in mistakes:
            mistakes[mistake_type].append(entry)
            mistakes[mistake_type] = mistakes[mistake_type][-50:]
        mistakes['total'] = mistakes.get('total', 0) + 1
        users_collection.update_one({'_id': user_id}, {'$set': {'mistakes': mistakes}})
    except Exception as e:
        print(f"Mistake log error: {e}")

def get_week_key():
    now = datetime.now()
    return f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"

def update_weekly_xp(user_id, xp_to_add):
    if users_collection is None or xp_to_add <= 0: return
    try:
        week = get_week_key()
        field = f'weekly_xp.{week}'
        users_collection.update_one({'_id': user_id}, {'$inc': {field: xp_to_add}})
    except Exception as e:
        print(f"Weekly XP update error: {e}")

def get_daily_challenges(user_id):
    if users_collection is None: return None
    try:
        user = users_collection.find_one({'_id': user_id})
        if not user: return None
        current_week = get_week_key()
        challenges = user.get('daily_challenges', {})
        if challenges.get('week') != current_week:
            streak = challenges.get('streak', 0)
            all_done = challenges.get('completed_today', 0) == 3
            new_streak = streak + 1 if all_done else 0
            challenges = {
                'week': current_week,
                'challenge1': {'type':'practice','target':5,'current':0,'completed':False,'xp':5},
                'challenge2': {'type':'learning','target':10,'current':0,'completed':False,'xp':5},
                'challenge3': {'type':'mastery','target':3,'current':0,'completed':False,'xp':5},
                'completed_today': 0,
                'streak': new_streak
            }
            if new_streak >= 7:
                users_collection.update_one({'_id': user_id}, {'$set': {'achievements.challenge_streak': new_streak}})
                check_and_award_badges(user_id)
            users_collection.update_one({'_id': user_id}, {'$set': {'daily_challenges': challenges}})
        return challenges
    except Exception as e:
        print(f"Weekly challenges error: {e}"); return None

def update_challenge_progress(user_id, challenge_type, increment=1):
    if users_collection is None: return 0
    try:
        challenges = get_daily_challenges(user_id)
        if not challenges: return 0
        xp_earned = 0
        type_map = {'practice': 'challenge1', 'learning': 'challenge2', 'mastery': 'challenge3'}
        key = type_map.get(challenge_type)
        if key and not challenges[key]['completed']:
            challenges[key]['current'] = min(challenges[key]['target'], challenges[key]['current'] + increment)
            if challenges[key]['current'] >= challenges[key]['target']:
                challenges[key]['completed'] = True
                challenges['completed_today'] = challenges.get('completed_today', 0) + 1
                xp_earned += challenges[key]['xp']
        if challenges.get('completed_today', 0) == 3:
            xp_earned += 10
        users_collection.update_one({'_id': user_id}, {'$set': {'daily_challenges': challenges}})
        if xp_earned > 0:
            cur_user = users_collection.find_one({'_id': user_id})
            if cur_user:
                new_xp = cur_user.get('total_xp', 0) + xp_earned
                new_level = calculate_level_from_xp(new_xp)
                users_collection.update_one({'_id': user_id}, {'$set': {'total_xp': new_xp, 'level': new_level}})
                check_and_award_badges(user_id)
        return xp_earned
    except Exception as e:
        print(f"Challenge update error: {e}"); return 0

def get_weekly_leaderboard(selected_class, selected_division=""):
    if users_collection is None: return []
    try:
        week = get_week_key()
        all_docs = list(users_collection.find({}))
        selected_class_str = str(selected_class).strip()
        selected_div_str = str(selected_division).strip().upper()
        students = []
        for s in all_docs:
            student_class = str(s.get('class', '')).strip()
            student_div   = str(s.get('division', '')).strip().upper()
            user_type     = s.get('user_type', 'student')
            if user_type == 'teacher': continue
            if student_class.lower() != selected_class_str.lower(): continue
            if selected_div_str and student_div != selected_div_str: continue
            students.append(s)
        lb = []
        for s in students:
            weekly_xp_map = s.get('weekly_xp', {})
            weekly_xp = weekly_xp_map.get(week, 0) if isinstance(weekly_xp_map, dict) else 0
            lb.append({
                'user_id': str(s.get('_id', '')),
                'name': s.get('name', s.get('username', 'Unknown')),
                'xp': s.get('total_xp', 0),
                'weekly_xp': weekly_xp,
                'level': s.get('level', 1),
                'badges': len(s.get('achievements', {}).get('badges_earned', [])),
                'division': s.get('division', '')
            })
        lb.sort(key=lambda x: (x['weekly_xp'], x['xp']), reverse=True)
        for i, e in enumerate(lb): e['rank'] = i + 1
        return lb
    except Exception as e:
        print(f"Leaderboard error: {e}"); return []

# ─── USER CRUD ───────────────────────────────────────────────────────────────

def create_user(user_id, username, password, user_type='student'):
    """Create a new student. Password is hashed automatically."""
    if users_collection is None: return None
    try:
        user = {
            '_id': user_id,
            'username': username,
            'password': hash_password(password),   # ← hashed
            'user_type': user_type,
            'total_xp': 0,
            'total_stars': 0,
            'level': 1,
            'created_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'last_active': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'mode_stats': {},
            'achievements': init_user_achievements(),
            'mistakes': {'pronunciation':[],'spelling':[],'vocabulary':[],'total':0},
            'daily_challenges': {},
            'security_question': None,    # set via /set-security-question
            'security_answer': None       # stored hashed
        }
        users_collection.insert_one(user)
        return user
    except DuplicateKeyError: return None
    except Exception as e: print(f"Create user error: {e}"); return None

def get_user_by_id(user_id):
    if users_collection is None: return None
    try: return users_collection.find_one({'_id': user_id})
    except Exception: return None

def get_user_by_username(username):
    if users_collection is None: return None
    try: return users_collection.find_one({'username': username})
    except Exception: return None

def update_user(user_id, update_data):
    if users_collection is None: return False
    try:
        update_data['last_active'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        users_collection.update_one({'_id': user_id}, {'$set': update_data})
        return True
    except Exception as e: print(f"Update user error: {e}"); return False

def rehash_user_password(user_id, plain_password):
    """Rehash a legacy plain-text password and save it."""
    if users_collection is None: return
    try:
        users_collection.update_one({'_id': user_id}, {'$set': {'password': hash_password(plain_password)}})
    except Exception as e:
        print(f"Rehash error: {e}")

def update_user_xp(user_id, xp, level, stars=None):
    if users_collection is None: return False
    try:
        data = {'total_xp': xp, 'level': level, 'last_active': datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        if stars is not None: data['total_stars'] = stars
        users_collection.update_one({'_id': user_id}, {'$set': data})
        check_and_award_badges(user_id)
        return True
    except Exception as e: print(f"Update XP error: {e}"); return False

def update_user_mode_stats(user_id, mode, stars_earned):
    if users_collection is None: return False
    try:
        users_collection.update_one(
            {'_id': user_id},
            {'$inc': {f'mode_stats.{mode}.stars': stars_earned, f'mode_stats.{mode}.sessions': 1},
             '$set': {'last_active': datetime.now().strftime("%Y-%m-%d %H:%M:%S")}},
            upsert=True
        )
        return True
    except Exception: return False

def get_all_users():
    if users_collection is None: return []
    try: return list(users_collection.find({'user_type': 'student'}))
    except Exception: return []

# ─── SECURITY QUESTION ──────────────────────────────────────────────────────

SECURITY_QUESTIONS = [
    "What is the name of your first pet?",
    "What is your favourite colour?",
    "What is the name of your best friend?",
    "What is your mother's first name?",
    "What city were you born in?",
    "What is your favourite food?",
    "What is the name of your school?",
    "What is your favourite animal?",
]

def set_security_question(user_id, question, answer_plain):
    """Save a student's security question with hashed answer."""
    if users_collection is None: return False
    try:
        users_collection.update_one(
            {'_id': user_id},
            {'$set': {
                'security_question': question,
                'security_answer': hash_password(answer_plain.strip().lower())
            }}
        )
        return True
    except Exception as e:
        print(f"Set security question error: {e}"); return False

def verify_security_answer(user_id, question, answer_plain):
    """
    Verify a student's security question answer.
    Returns True only if both the question AND the answer match.
    """
    if users_collection is None: return False
    try:
        user = users_collection.find_one({'_id': user_id})
        if not user: return False
        if user.get('security_question') != question: return False
        stored_answer = user.get('security_answer')
        if not stored_answer: return False
        return check_pw(stored_answer, answer_plain.strip().lower())
    except Exception as e:
        print(f"Verify security answer error: {e}"); return False

def reset_student_password_by_security(user_id, new_password):
    """Self-service password reset after security question verification."""
    if users_collection is None: return False
    try:
        users_collection.update_one(
            {'_id': user_id},
            {'$set': {'password': hash_password(new_password)}}
        )
        return True
    except Exception as e:
        print(f"Self-service reset error: {e}"); return False

def get_user_security_question(user_id):
    """Returns the question string or None if not set."""
    if users_collection is None: return None
    try:
        user = users_collection.find_one({'_id': user_id})
        return user.get('security_question') if user else None
    except Exception:
        return None

# ─── TEACHER CRUD ──────────────────────────────────────────────────────────

def create_teacher(teacher_id, username, password):
    """Password is hashed automatically."""
    if teachers_collection is None: return None
    try:
        teacher = {
            '_id': teacher_id,
            'username': username,
            'password': hash_password(password),   # ← hashed
            'created_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'last_active': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        teachers_collection.insert_one(teacher)
        return teacher
    except DuplicateKeyError: return None
    except Exception as e: print(f"Create teacher error: {e}"); return None

def get_teacher_by_username(username):
    if teachers_collection is None: return None
    try: return teachers_collection.find_one({'username': username})
    except Exception: return None

def get_teacher_by_id(teacher_id):
    if teachers_collection is None: return None
    try: return teachers_collection.find_one({'_id': teacher_id})
    except Exception: return None

def update_teacher(teacher_id, update_data):
    if teachers_collection is None: return False
    try:
        update_data['last_active'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        teachers_collection.update_one({'_id': teacher_id}, {'$set': update_data})
        return True
    except Exception: return False

def rehash_teacher_password(teacher_id, plain_password):
    """Rehash a legacy plain-text teacher password."""
    if teachers_collection is None: return
    try:
        teachers_collection.update_one({'_id': teacher_id}, {'$set': {'password': hash_password(plain_password)}})
    except Exception as e:
        print(f"Teacher rehash error: {e}")

# ─── TEACHER PASSWORD RESET REQUESTS ────────────────────────────────────────

def request_teacher_password_reset(teacher_id):
    """Teacher submits a password reset request visible to admin."""
    if teachers_collection is None: return False
    try:
        teachers_collection.update_one(
            {'_id': teacher_id},
            {'$set': {
                'password_reset_requested': True,
                'password_reset_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }}
        )
        return True
    except Exception as e:
        print(f"Teacher password reset request error: {e}"); return False

def get_teachers_requesting_password_reset():
    """Admin — get all teachers who have a pending password reset request."""
    if teachers_collection is None: return []
    try:
        return list(teachers_collection.find({'password_reset_requested': True}))
    except Exception as e:
        print(f"Get reset requests error: {e}"); return []

def clear_password_reset_request(teacher_id):
    """Admin — clear the flag after resetting the password."""
    if teachers_collection is None: return False
    try:
        teachers_collection.update_one(
            {'_id': teacher_id},
            {'$unset': {'password_reset_requested': '', 'password_reset_at': ''}}
        )
        return True
    except Exception: return False

# ─── CONVERSATION ────────────────────────────────────────────────────────────

def save_conversation(user_id, mode, conversation_text):
    """
    Save conversation context with per-mode trimming:
      conversation       → keep last 100 lines (50 exchanges)
      roleplay_<type>    → keep last 50 lines  (25 exchanges)
    """
    if conversations_collection is None: return False
    try:
        msgs = conversation_text.strip().split('\n') if conversation_text else []
        # Determine trim limit by mode
        if mode == 'conversation':
            limit = 100
        else:
            limit = 50   # all roleplay_* modes
        if len(msgs) > limit:
            conversation_text = '\n'.join(msgs[-limit:])
        conversations_collection.update_one(
            {'user_id': user_id},
            {'$set': {
                f'conversations.{mode}': conversation_text,
                'updated_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }},
            upsert=True
        )
        return True
    except Exception as e:
        print(f"Save conv error: {e}"); return False

def get_conversation(user_id, mode):
    if conversations_collection is None: return ''
    try:
        doc = conversations_collection.find_one({'user_id': user_id})
        if doc and 'conversations' in doc:
            return doc['conversations'].get(mode, '')
        return ''
    except Exception: return ''

def delete_conversation(user_id, mode):
    if conversations_collection is None: return False
    try:
        conversations_collection.update_one(
            {'user_id': user_id},
            {'$unset': {f'conversations.{mode}': ''}}
        )
        return True
    except Exception: return False

# ─── DB UTILS ────────────────────────────────────────────────────────────────

def check_connection():
    return client is not None and db is not None

def get_database_stats():
    if db is None: return None
    try:
        return {
            'users': users_collection.count_documents({}) if users_collection is not None else 0,
            'teachers': teachers_collection.count_documents({}) if teachers_collection is not None else 0
        }
    except Exception: return None

# ─── ADMIN CRUD ──────────────────────────────────────────────────────────────

def create_admin(admin_id, username, password, pre_hashed=False):
    """Create admin. Pass pre_hashed=True if password is already hashed."""
    if admins_collection is None: return None
    try:
        admin = {
            '_id': admin_id,
            'username': username,
            'password': password if pre_hashed else hash_password(password),
            'role': 'admin',
            'created_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'last_active': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        admins_collection.insert_one(admin)
        return admin
    except DuplicateKeyError: return None
    except Exception as e: print(f"Create admin error: {e}"); return None

def get_admin_by_username(username):
    if admins_collection is None: return None
    try: return admins_collection.find_one({'username': username})
    except Exception: return None

def get_admin_by_id(admin_id):
    if admins_collection is None: return None
    try: return admins_collection.find_one({'_id': admin_id})
    except Exception: return None

def update_admin(admin_id, update_data):
    if admins_collection is None: return False
    try:
        update_data['last_active'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        admins_collection.update_one({'_id': admin_id}, {'$set': update_data})
        return True
    except Exception: return False

def ensure_default_admin():
    """Create the default admin account if none exists (password is hashed)."""
    if admins_collection is None: return
    try:
        if admins_collection.count_documents({}) == 0:
            create_admin('admin_001', 'admin', 'admin123')
            print("[ADMIN] Default admin created: username=admin, password=admin123")
        else:
            # Migrate any plain-text admin passwords
            for admin in admins_collection.find({}):
                pw = admin.get('password', '')
                if pw and not is_hashed(pw):
                    admins_collection.update_one(
                        {'_id': admin['_id']},
                        {'$set': {'password': hash_password(pw)}}
                    )
                    print(f"[ADMIN] Rehashed password for admin: {admin.get('username')}")
            count = admins_collection.count_documents({})
            print(f"[ADMIN] {count} admin(s) found.")
    except Exception as e:
        print(f"[ADMIN] Error ensuring default admin: {e}")

# ─── TEACHER SIGNUP REQUESTS ─────────────────────────────────────────────────

def create_teacher_request(teacher_id, username, password, name):
    """Pending teacher signup — password is hashed on save."""
    if teachers_collection is None: return None
    try:
        teacher = {
            '_id': teacher_id,
            'username': username,
            'password': hash_password(password),   # ← hashed
            'name': name,
            'status': 'pending',
            'created_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'last_active': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        teachers_collection.insert_one(teacher)
        return teacher
    except DuplicateKeyError: return None
    except Exception as e: print(f"Create teacher request error: {e}"); return None

def approve_teacher(teacher_id):
    if teachers_collection is None: return False
    try:
        teachers_collection.update_one({'_id': teacher_id}, {'$set': {'status': 'approved'}})
        return True
    except Exception: return False

def reject_teacher(teacher_id):
    if teachers_collection is None: return False
    try:
        teachers_collection.update_one({'_id': teacher_id}, {'$set': {'status': 'rejected'}})
        return True
    except Exception: return False

def get_all_teachers():
    if teachers_collection is None: return []
    try: return list(teachers_collection.find({}))
    except Exception: return []

def get_pending_teachers():
    if teachers_collection is None: return []
    try: return list(teachers_collection.find({'status': 'pending'}))
    except Exception: return []

def delete_teacher(teacher_id):
    if teachers_collection is None: return False
    try:
        teachers_collection.delete_one({'_id': teacher_id})
        return True
    except Exception: return False

def delete_user(user_id):
    if users_collection is None: return False
    try:
        users_collection.delete_one({'_id': user_id})
        return True
    except Exception: return False

def admin_reset_user_password(user_id, new_password):
    """Admin resets student password — new password is hashed."""
    if users_collection is None: return False
    try:
        users_collection.update_one(
            {'_id': user_id},
            {'$set': {'password': hash_password(new_password)}}
        )
        return True
    except Exception: return False

def admin_reset_teacher_password(teacher_id, new_password):
    """Admin resets teacher password — new password is hashed and reset request cleared."""
    if teachers_collection is None: return False
    try:
        teachers_collection.update_one(
            {'_id': teacher_id},
            {'$set': {'password': hash_password(new_password)},
             '$unset': {'password_reset_requested': '', 'password_reset_at': ''}}
        )
        return True
    except Exception: return False

# ─── MIGRATION HELPERS ───────────────────────────────────────────────────────

def migrate_all_users_levels_and_badges():
    if users_collection is None:
        print("Migration skipped – no DB connection.")
        return {'migrated': 0, 'errors': 0}
    migrated = 0; errors = 0
    try:
        all_users = list(users_collection.find({'user_type': {'$ne': 'teacher'}}))
        for user in all_users:
            try:
                uid = user['_id']
                correct_level = calculate_level_from_xp(user.get('total_xp', 0))
                updates = {'level': correct_level}
                achievements = user.get('achievements', init_user_achievements())
                if not isinstance(achievements, dict):
                    updates['achievements'] = init_user_achievements()
                # Ensure security fields exist for old accounts
                if 'security_question' not in user:
                    updates['security_question'] = None
                if 'security_answer' not in user:
                    updates['security_answer'] = None
                users_collection.update_one({'_id': uid}, {'$set': updates})
                check_and_award_badges(uid)
                migrated += 1
            except Exception as e:
                print(f"Migration error for user {user.get('_id')}: {e}"); errors += 1
    except Exception as e:
        print(f"Migration fetch error: {e}"); errors += 1
    print(f"[MIGRATION] Levels + badges for {migrated} users ({errors} errors).")
    return {'migrated': migrated, 'errors': errors}

def backfill_weekly_xp():
    if users_collection is None: return
    try:
        week = get_week_key()
        field = f'weekly_xp.{week}'
        seeded = 0
        for user in list(users_collection.find({'user_type': {'$ne': 'teacher'}})):
            weekly_xp_map = user.get('weekly_xp', {})
            if not isinstance(weekly_xp_map, dict) or week not in weekly_xp_map:
                total_xp = user.get('total_xp', 0)
                if total_xp > 0:
                    users_collection.update_one({'_id': user['_id']}, {'$set': {field: total_xp}})
                    seeded += 1
        if seeded:
            print(f"[BACKFILL] Seeded weekly XP for {seeded} students for week {week}")
    except Exception as e:
        print(f"[BACKFILL] Error: {e}")

if __name__ != "__main__":
    if check_connection():
        stats = get_database_stats()
        if stats:
            print(f"DB Stats: {stats['users']} users, {stats['teachers']} teachers")
