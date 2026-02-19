from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import os
from dotenv import load_dotenv
from gtts import gTTS
from difflib import SequenceMatcher
from groq import Groq
import uuid
import re
import json
from datetime import datetime
import random
import atexit
import signal
import secrets

# Import database functions
from werkzeug.security import check_password_hash
from database import (
    create_user, get_user_by_id, get_user_by_username,
    update_user, update_user_xp, update_user_mode_stats, get_all_users,
    create_teacher, get_teacher_by_username, get_teacher_by_id, update_teacher,
    save_conversation, get_conversation, delete_conversation,
    check_connection, get_database_stats,
    update_login_streak, increment_activity, log_mistake,
    get_daily_challenges, update_challenge_progress,
    get_weekly_leaderboard, check_and_award_badges, ALL_BADGES,
    xp_threshold_for_level, calculate_level_from_xp,
    migrate_all_users_levels_and_badges, update_weekly_xp, backfill_weekly_xp,
    # Auth helpers
    check_pw, is_hashed, rehash_user_password, rehash_teacher_password,
    # Security question
    set_security_question, verify_security_answer, reset_student_password_by_security,
    get_user_security_question, SECURITY_QUESTIONS,
    # Teacher password reset
    request_teacher_password_reset, get_teachers_requesting_password_reset,
    clear_password_reset_request,
    # Admin functions
    create_admin, get_admin_by_username, get_admin_by_id, update_admin, ensure_default_admin,
    create_teacher_request, approve_teacher, reject_teacher,
    get_all_teachers, get_pending_teachers, delete_teacher,
    delete_user, admin_reset_user_password, admin_reset_teacher_password
)

# ================= SETUP =================
load_dotenv()
client = Groq(api_key=os.getenv("GROQ_API_KEY"))

app = Flask(__name__)
# Use environment variable or generate secure random key
app.secret_key = os.getenv('FLASK_SECRET_KEY', secrets.token_hex(32))

# Separate conversation contexts for each mode (now using MongoDB)
conversation_contexts = {}  # Format: {user_id: {'conversation': '', 'roleplay': ''}}

# OLD: User database - NOW USING MONGODB (kept for backwards compatibility)
users_db = {}

# OLD: Teacher database - NOW USING MONGODB (kept for backwards compatibility)
teachers_db = {}

# Print MongoDB connection status
print("=" * 60)
if check_connection():
    stats = get_database_stats()
    if stats:
        print(f"✅ MongoDB Connected!")
        print(f"📊 Database: {stats['users']} users, {stats['teachers']} teachers")
    else:
        print("✅ MongoDB Connected (empty database)")
    # ── Run one-shot migration: fix levels + assign missing badges ──
    migrate_all_users_levels_and_badges()
    # ── Backfill weekly XP for existing students ──
    backfill_weekly_xp()
    # ── Ensure default admin account exists ──
    ensure_default_admin()
else:
    print("⚠️ MongoDB NOT connected - data will NOT persist!")
    print("⚠️ Check MONGODB_URI environment variable")
print("=" * 60)

# Progressive XP requirements
# Level 1→2: 25 XP, 2→3: 50 XP, 3→4: 100 XP, 4→5: 200 XP … (doubles each level)
def get_xp_for_level(level):
    """Total XP required to reach `level` (delegates to database module)."""
    return xp_threshold_for_level(level)

def calculate_level(xp):
    """Calculate level based on current XP (delegates to database module)."""
    return calculate_level_from_xp(xp)

def get_xp_for_next_level(current_level):
    """XP gap between current level and next level."""
    return xp_threshold_for_level(current_level + 1) - xp_threshold_for_level(current_level)

def get_difficulty_for_level(level):
    """Auto-adjust difficulty based on level"""
    if level <= 2:
        return "easy"
    elif level <= 4:
        return "easy"
    elif level <= 7:
        return "medium"
    elif level <= 10:
        return "medium"
    else:
        return "hard"

def save_user_progress(user_id, stars_earned, mode):
    """Save user progress and update XP using MongoDB"""
    user = get_user_by_id(user_id)
    
    if user:
        old_level = user.get('level', 1)
        old_xp = user.get('total_xp', 0)
        old_stars = user.get('total_stars', 0)
        
        new_xp = old_xp + stars_earned
        new_stars = old_stars + stars_earned
        new_level = calculate_level(new_xp)
        
        # Update user XP and level in MongoDB
        update_user_xp(user_id, new_xp, new_level, new_stars)
        
        # Track XP earned this week (for weekly leaderboard)
        update_weekly_xp(user_id, stars_earned)
        
        # Update mode stats in MongoDB
        update_user_mode_stats(user_id, mode, stars_earned)
        
        # Track activity for achievements
        if mode == 'repeat':
            increment_activity(user_id, 'repeat')
            update_challenge_progress(user_id, 'learning')
        elif mode == 'spellbee':
            if stars_earned > 0:
                increment_activity(user_id, 'spelling')
        elif mode == 'conversation':
            increment_activity(user_id, 'conversation')
            update_challenge_progress(user_id, 'practice')
        elif mode == 'roleplay':
            increment_activity(user_id, 'roleplay')
            update_challenge_progress(user_id, 'practice')
        
        return {
            'leveled_up': new_level > old_level,
            'new_level': new_level,
            'old_level': old_level
        }
    return None

def save_database():
    """Save databases to JSON files with improved error handling"""
    try:
        # Save users database
        with open('users_data.json', 'w', encoding='utf-8') as f:
            json.dump(users_db, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        
        # Save teachers database
        with open('teachers_data.json', 'w', encoding='utf-8') as f:
            json.dump(teachers_db, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        
        print(f"Database saved successfully at {datetime.now()}")
        print(f"Users in DB: {len(users_db)}, Teachers in DB: {len(teachers_db)}")
    except Exception as e:
        print(f"ERROR saving database: {e}")
        import traceback
        traceback.print_exc()

def load_database():
    """Load databases from JSON files with improved error handling"""
    global users_db, teachers_db
    try:
        if os.path.exists('users_data.json'):
            with open('users_data.json', 'r', encoding='utf-8') as f:
                loaded_users = json.load(f)
                users_db = loaded_users
                print(f"Loaded {len(users_db)} users from database")
        else:
            print("No users_data.json found, starting with empty database")
            users_db = {}
        
        if os.path.exists('teachers_data.json'):
            with open('teachers_data.json', 'r', encoding='utf-8') as f:
                loaded_teachers = json.load(f)
                teachers_db = loaded_teachers
                print(f"Loaded {len(teachers_db)} teachers from database")
        else:
            print("No teachers_data.json found, starting with empty database")
            teachers_db = {}
    except Exception as e:
        print(f"ERROR loading database: {e}")
        import traceback
        traceback.print_exc()
        users_db = {}
        teachers_db = {}

load_database()

# Register cleanup handlers to save on exit
def cleanup_handler(signum=None, frame=None):
    """Save database on program exit"""
    print("Saving database before exit...")
    save_database()

atexit.register(cleanup_handler)
signal.signal(signal.SIGTERM, cleanup_handler)
signal.signal(signal.SIGINT, cleanup_handler)

def get_user_context(user_id, mode):
    """Get conversation context for specific user and mode from MongoDB"""
    # Try MongoDB first
    context = get_conversation(user_id, mode)
    if context:
        return context
    
    # Fallback to in-memory (for migration period)
    if user_id not in conversation_contexts:
        conversation_contexts[user_id] = {'conversation': '', 'roleplay': ''}
    return conversation_contexts[user_id].get(mode, '')

def update_user_context(user_id, mode, context):
    """Update conversation context. Trimming is handled inside save_conversation by mode:
       conversation = 100 lines (50 exchanges)
       roleplay_*   =  50 lines (25 exchanges per role)"""
    # Save to MongoDB (trimming happens in save_conversation based on mode)
    save_conversation(user_id, mode, context)
    
    # Also update in-memory cache
    if user_id not in conversation_contexts:
        conversation_contexts[user_id] = {}
    conversation_contexts[user_id][mode] = context


# ================= TTS =================
def speak_to_file(text, slow=False):
    os.makedirs("static/audio", exist_ok=True)
    filename = f"{uuid.uuid4()}.mp3"
    path = f"static/audio/{filename}"
    gTTS(text=text, lang="en", slow=slow).save(path)
    return "/" + path

# ================= AI FUNCTIONS WITH ISOLATED MEMORY =================

def english_coach(child_text, user_id):
    """Conversation mode with isolated memory per user"""
    context = get_user_context(user_id, 'conversation')
    
    prompt_variations = [
        "make the response natural and conversational",
        "use different words than previous responses",
        "be creative with your follow-up question",
        "vary your praise words",
        "ask about different topics each time"
    ]
    variation_hint = random.choice(prompt_variations)

    prompt = f"""
You are an English speaking coach for children aged 6 to 15.

STRICT RULES:
- Always correct the child's sentence
- If only one word, make a full sentence
- Very simple English
- Encourage the child with VARIED praise words
- Ask ONE follow-up question about DIFFERENT topics each time
- No grammar explanation
- {variation_hint}

Respond ONLY in this format:

CORRECT: <correct sentence>
PRAISE: <short encouragement - use different words>
QUESTION: <one simple question about a NEW topic>

Conversation so far:
{context}

Child says:
"{child_text}"
"""

    response = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
        top_p=0.9
    )

    reply = response.choices[0].message.content.strip()
    new_context = context + f"\nChild: {child_text}\nAssistant: {reply}"
    update_user_context(user_id, 'conversation', new_context)
    
    return reply

def roleplay_coach(child_text, roleplay_type, user_id):
    """Roleplay mode with isolated memory per user"""
    # Use per-roleplay-type context so teacher/friend/interviewer/viva never share memory
    context_key = f'roleplay_{roleplay_type}' if roleplay_type else 'roleplay'
    context = get_user_context(user_id, context_key)

    # UPDATED: Title-specific roles with domain-specific questions
    roles = {
        "teacher": """
You are a kind school teacher.
Help the student learn English.
Ask VARIED study-related questions SPECIFICALLY about:
- Different academic subjects (Math, Science, History, Geography, Literature)
- Study habits and homework
- School projects and assignments
- Educational goals and interests
- Learning challenges and achievements
Each question should be about a DIFFERENT academic topic.
Be encouraging and patient.
Stay strictly in teacher role.
""",
        "friend": """
You are a friendly classmate.
Talk casually and happily.
Ask about DIFFERENT personal topics SPECIFICALLY like:
- Favorite hobbies and activities
- Weekend plans and adventures
- Favorite games, movies, or books
- Sports and outdoor activities
- Personal interests and collections
- Family activities and pets
Each question should be about a DIFFERENT casual topic.
Be cheerful and supportive.
Stay strictly in friend role.
""",
        "interviewer": """
You are a job interviewer.
Be polite and professional.
Ask DIFFERENT professional questions SPECIFICALLY about:
- Career goals and aspirations
- Skills and strengths
- Work experience (even if limited)
- Problem-solving abilities
- Teamwork and leadership
- Future plans and ambitions
Each question should be a DIFFERENT interview topic.
Be encouraging but maintain professional tone.
Stay strictly in interviewer role.
""",
        "viva": """
You are a viva examiner.
Ask DIFFERENT academic project questions SPECIFICALLY about:
- Project objectives and goals
- Research methodology
- Findings and results
- Challenges faced
- Applications and implications
- Future scope and improvements
Each question should probe a DIFFERENT aspect of academic work.
Focus on understanding various project dimensions.
Be fair and encouraging while maintaining examiner professionalism.
Stay strictly in viva examiner role.
"""
    }

    role_instruction = roles.get(roleplay_type, "You are a friendly English speaking partner.")
    variety_hints = [
        "Ask about something you haven't asked before in this conversation",
        "Use different question words than your previous questions",
        "Focus on a completely different aspect of your role",
        "Be creative and probe a new dimension",
        "Explore an unexplored area relevant to your role"
    ]
    variety_hint = random.choice(variety_hints)

    prompt = f"""
{role_instruction}

You are doing roleplay with a student aged 6 to 15.

STRICT RULES:
- Always correct the student's sentence
- Very simple English
- Stay STRICTLY in your role
- Ask questions ONLY related to your specific role domain
- Encourage the student with VARIED praise
- Ask ONE role-specific question from your domain
- No grammar explanation
- {variety_hint}

Respond ONLY in this format:

CORRECT: <correct sentence>
PRAISE: <short encouragement - vary your words>
QUESTION: <one role-specific question about a NEW topic from your domain>

Conversation so far:
{context}

Student says:
"{child_text}"
"""

    response = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
        top_p=0.9
    )

    reply = response.choices[0].message.content.strip()
    new_context = context + f"\nStudent: {child_text}\nAssistant: {reply}"
    update_user_context(user_id, context_key, new_context)
    
    return reply

# ================= REPEAT & SPELL BEE FUNCTIONS =================
def generate_repeat_sentence(category="general", difficulty="easy", user_level=1):
    """Generate sentences with 150+ options per difficulty - NO AI, pure random selection"""
    
    # Use the difficulty selected by the user directly - no level-based override
    actual_difficulty = difficulty
    
    # MASSIVELY EXPANDED SENTENCE POOLS - 150+ per difficulty level
    # Easy: 3-5 words | Medium: 8-15 words | Hard: 18-32 words
    
    # CIVIC SENSE CATEGORY - Teaching good citizenship and community behavior
    all_easy_sentences = [
        # 3-5 word sentences (150 total) - Basic civic responsibilities
        "Keep our city clean", "Respect all traffic lights", "Wait in proper queues", "Help elderly people always",
        "Use dustbins for waste", "Don't litter on streets", "Be polite to everyone", "Follow all safety rules",
        "Obey traffic police orders", "Save water every day", "Plant more green trees", "Keep parks very clean",
        "Respect public property always", "Don't waste any food", "Share with needy people", "Be kind to animals",
        "Cross roads very safely", "Keep toilets very clean", "Turn off unused lights", "Speak softly in public",
        "I drink cold water", "She eats her lunch", "He rides a bike", "They watch the TV",
        "We go back home", "Birds fly to south", "Bells ring very loud", "Doors open really wide",
        "Windows are very clean", "Lights turn on now", "Music sounds really good", "Food tastes quite great",
        "Air smells so fresh", "Grass feels very soft", "Ice is very cold", "Fire burns so hot",
        "Books help us learn", "Pens write very well", "Paper is quite thin", "Glue sticks things tight",
        "Scissors cut the paper", "Crayons draw nice pictures", "Paint is very colorful", "Brushes are quite soft",
        "Cups hold the water", "Plates hold the food", "Spoons are very helpful", "Forks work quite well",
        "Knives cut the bread", "Bowls are very round", "Pots cook the food", "Pans fry the eggs",
        "Beds are very soft", "Pillows feel so nice", "Blankets keep us warm", "Sheets are very clean",
        "Chairs support us well", "Tables hold many things", "Lamps give bright light", "Fans cool the rooms",
        "Clocks tell the time", "Watches show the hours", "Calendars show all dates", "Maps show many places",
        "Pictures look very nice", "Mirrors reflect the light", "Carpets cover the floors", "Curtains block the sun",
        "Plants need fresh water", "Gardens grow good food", "Seeds become tall plants", "Fruits taste very sweet",
        "Vegetables are quite healthy", "Bread is very soft", "Cheese tastes really good", "Milk builds strong bones",
        "Water quenches our thirst", "Juice is very sweet", "Tea is quite warm", "Coffee smells really good",
        "Cars move very fast", "Buses carry many people", "Trains run quite long", "Planes fly up high",
        "Boats float on water", "Ships are very big", "Trucks haul heavy cargo", "Bikes save good energy",
        "Roads connect many places", "Bridges cross big rivers", "Tunnels go straight through", "Streets are very busy",
        "Stores sell many things", "Markets are quite crowded", "Shops have good goods", "Malls are very large",
        "Schools teach all students", "Teachers help us learn", "Books contain much knowledge", "Pencils write the words",
        "Erasers remove the mistakes", "Rulers measure the length", "Compasses draw nice circles", "Calculators solve hard math",
        "Computers process the data", "Keyboards type the letters", "Mice click the buttons", "Screens display the information",
        "Phones make the calls", "Radios play good music", "Cameras take nice pictures", "Videos show good motion",
        "Letters deliver the messages", "Packages contain the items", "Envelopes hold the letters", "Stamps cost some money",
        "Money buys many things", "Coins are made metal", "Bills are made paper", "Banks keep things safe",
        "Doctors treat the patients", "Nurses care for people", "Hospitals heal sick people", "Medicine makes us better",
        "Police keep us safe", "Firefighters stop big fires", "Soldiers protect our country", "Heroes save many lives",
        "Farmers grow good crops", "Workers build many things", "Artists create such beauty", "Musicians make good music",
        "Dancers move very gracefully", "Singers have nice voices", "Actors perform great plays", "Writers pen good stories",
        "Cooks prepare the meals", "Bakers make fresh bread", "Chefs create nice dishes", "Waiters serve the food",
        "Pilots fly the planes", "Sailors navigate the ships", "Drivers steer the vehicles", "Captains lead the teams",
        "Friends share their toys", "Families eat together daily", "Neighbors help each other", "People work very hard"
    ]
    
    all_medium_sentences = [
        # 8-12 word sentences (150 total)
        "I always brush my teeth carefully every single morning before going to school",
        "The beautiful blue sky looks absolutely stunning today with no clouds at all",
        "My best friend always helps me complete my homework assignments after school ends",
        "We enjoy watching interesting movies together every single weekend at our comfortable home",
        "The public library has thousands of fascinating books available for everyone to read freely",
        "I practice playing the piano diligently right after school ends every single day",
        "My grandmother tells wonderful bedtime stories every night before we go to sleep peacefully",
        "The grocery store always sells fresh vegetables and fruits every single day without fail",
        "Children play happily together in the park every sunny afternoon outside with their friends",
        "Our dedicated teacher explains difficult lessons very clearly to all students in the classroom",
        "The mailman delivers letters and packages every afternoon punctually at three o'clock sharp each day",
        "My talented sister bakes delicious chocolate chip cookies for us very often at home",
        "The beautiful garden flowers bloom magnificently during the lovely springtime every single year naturally",
        "Students study very hard for all their important exams regularly throughout the entire academic year",
        "Basketball players practice their shooting skills diligently every single day after school in the gym",
        "The kind librarian helps students find interesting books in the very large library every day",
        "We celebrate birthdays with cake and candles together joyfully as a happy loving family",
        "Colorful butterflies dance gracefully around the blooming flowers in the beautiful garden outside our house",
        "My energetic puppy loves playing fetch in the backyard every single afternoon when weather permits",
        "The school cafeteria serves hot nutritious meals to all students every single day at lunchtime",
        "Brave firefighters respond quickly to all emergency calls immediately without any delay or hesitation whatsoever",
        "The museum displays ancient artifacts that are carefully preserved for all future generations to see",
        "Local farmers sell fresh organic vegetables at the farmers market every weekend to local residents",
        "My uncle fixes broken computers very skillfully in his workshop every day with special tools",
        "The community swimming pool opens early during the warm summer months for all swimmers daily",
        "We recycle plastic bottles regularly to help protect our precious environment for all future generations",
        "The friendly dentist checks teeth for cavities twice every year very carefully and thoroughly always",
        "My cousin collects colorful stamps from many different countries around the world quite enthusiastically regularly",
        "The dedicated park ranger protects wildlife in the forest every single day rain or shine",
        "We plant trees regularly to make our city much greener for everyone living here today",
        "The busy bakery opens at six o'clock every single morning selling delicious fresh bread daily",
        "Doctors recommend eating fruits and vegetables daily regularly for maintaining good health and wellness always",
        "The traffic light helps people cross busy streets safely every single day without any accidents",
        "Astronauts train for many years before going to space on exciting special dangerous missions regularly",
        "My neighbor waters her beautiful garden every single evening after work ends without fail daily",
        "The skilled mechanic repairs cars in his busy workshop throughout the day using various tools",
        "Librarians organize books alphabetically on tall wooden shelves in the large library very carefully always",
        "We save money in piggy banks for our future dreams and important goals every week",
        "The postman delivers packages rain or shine very faithfully every single afternoon at three o'clock",
        "Scientists conduct important experiments in their laboratories very carefully every single day to discover new things",
        "Athletes warm up properly before competing in big tournaments to prevent any possible physical injuries",
        "The veterinarian treats sick animals with gentle care in the animal clinic every day carefully",
        "Musicians practice scales and songs many hours every single day to improve their musical skills",
        "The janitor cleans classrooms thoroughly every single evening after students leave school for the day",
        "Photographers capture beautiful moments with their expensive digital cameras at special events and celebrations always",
        "The conductor leads the orchestra through complex symphonies during concert performances every night with precision",
        "Volunteers help organize community events for free very willingly to help other people in need",
        "The architect designs buildings using special computer programs in the office every day for clients",
        "Lifeguards watch swimmers carefully at the busy crowded beach during summer vacation months every day",
        "The florist arranges beautiful bouquets for special occasions like weddings and birthdays every single day",
        "Engineers solve difficult problems using mathematics and physics every day in their office workspace carefully",
        "The jeweler repairs watches and necklaces very carefully in the small workshop using delicate tools",
        "Pilots check aircraft systems before every single flight to ensure passenger safety always without exception",
        "The tailor sews custom clothes using traditional techniques in the shop every day for customers",
        "Electricians install wiring in new houses very safely following all building codes strictly every day",
        "The optometrist tests vision and prescribes eyeglasses accurately for patients every single day at the clinic",
        "Geologists study rocks and minerals from different regions around the world every day in laboratories",
        "The zookeeper feeds animals according to special schedules in the zoo every day without missing",
        "Carpenters build furniture using quality wood and tools in workshops throughout the day for clients",
        "The locksmith makes duplicate keys very precisely using special machines every single day for customers",
        "Translators convert text from one language to another accurately for business clients every day carefully",
        "The choreographer creates beautiful dance routines for performances with dancers every single week for shows",
        "Journalists write news articles about current important events happening around the world every single day",
        "The accountant manages financial records using special software in the office every day for companies",
        "Historians research past events using ancient documents very carefully in libraries and archives every day",
        "The therapist helps people overcome their personal challenges through counseling sessions regularly every week carefully",
        "Programmers write complex computer code for useful applications on computers every single day at work",
        "The sommelier recommends perfect wines for gourmet meals at expensive restaurants every evening to customers",
        "Astronomers observe distant stars through powerful telescopes at the observatory every single night when clear",
        "The curator preserves valuable artwork in the museum with great care every day for posterity",
        "Botanists study different plant species in tropical rainforests on research expeditions regularly every year carefully",
        "The conductor ensures trains depart on schedule punctually from the station every single day without delays",
        "Surgeons perform delicate operations in sterile operating rooms at hospitals every single day to save lives",
        "The decorator arranges furniture to maximize room space in houses for clients regularly every week",
        "Nutritionists plan healthy balanced meals for various clients with special dietary needs every day carefully",
        "The auctioneer sells valuable items to highest bidders at auctions every single weekend with enthusiasm",
        "Zoologists observe animal behavior in their natural habitats on research expeditions every year in forests",
        "The barista prepares specialty coffee drinks with skill at the cafe every morning for customers",
        "Psychologists help patients understand their emotions and thoughts during therapy sessions every week with compassion",
        "The landscaper designs beautiful gardens using native plants for homeowners in the area every season",
        "Astronomers calculate distances to faraway galaxies using mathematics at the observatory every night when possible",
        "The receptionist greets visitors with friendly warm smiles at the office every day without fail",
        "Archaeologists excavate ancient ruins very carefully with brushes on research expeditions every summer in deserts",
        "The coach motivates athletes to achieve their best performance during practice sessions every day passionately",
        "Paramedics provide emergency medical care at accident scenes throughout the city every day to victims",
        "The editor reviews manuscripts for grammar and clarity for publishing companies every week very carefully",
        "Marine biologists study ocean creatures in deep waters on research vessels regularly throughout the year",
        "The conductor keeps musicians playing together in harmony during orchestra performances every night with skill",
        "Geographers create detailed maps showing terrain and features using modern technology every day in offices",
        "The referee enforces rules fairly during sports games to ensure fair play always without bias",
        "Paleontologists discover dinosaur fossils buried underground for millions of years on expeditions regularly every summer",
        "The counselor helps students choose appropriate career paths for their future success in life carefully",
        "Ornithologists study bird migration patterns across multiple continents on research projects every year with dedication",
        "The sommelier pairs wines with dishes perfectly every time at fancy restaurants for customers",
        "Meteorologists forecast weather patterns using satellite data at the station every single day accurately",
        "The librarian organizes book collections and helps visitors find resources every day with patience",
        "Chefs prepare gourmet meals using fresh ingredients in restaurant kitchens every evening for diners",
        "The pharmacist fills prescriptions accurately and provides medical advice to patients every day at drugstores",
        "Teachers create lesson plans and instruct students in classrooms five days every week patiently",
        "The mechanic diagnoses car problems using diagnostic tools in the shop every day for clients",
        "Nurses care for patients and administer medications in hospitals twenty four hours every day",
        "The gardener maintains landscapes and prunes plants for clients throughout the week regularly with care",
        "Dentists examine teeth and perform procedures in clinics five days every single week for patients",
        "The artist creates paintings and sculptures in the studio workspace every single day with passion",
        "Pilots navigate aircraft safely through airspace on scheduled flights every single day for passengers",
        "The writer composes articles and stories on the computer every day for various publications regularly",
        "Engineers design structures and systems using computer programs in offices every working day for projects",
        "The security guard patrols buildings protecting property from theft every single night throughout the area",
        "Coaches train athletes in various sports teaching them techniques and strategies every day at gyms",
        "The florist creates beautiful arrangements combining flowers and greenery for events every week with creativity",
        "Mechanics replace worn parts and service vehicles in repair shops every day for different customers",
        "The tour guide shows visitors historical sites explaining their significance every day with great enthusiasm",
        "Bankers manage accounts and provide financial advice to clients every day at the branch office",
        "The seamstress alters clothing ensuring perfect fits for customers every day in the small shop",
        "Firefighters train regularly practicing rescue techniques to stay prepared for emergencies every single week diligently",
        "The plumber fixes leaks and installs fixtures in homes and businesses every day using tools",
        "Veterinary technicians assist doctors caring for animals in clinics every day with gentle hands",
        "The electrician troubleshoots wiring problems and makes repairs safely every day for various clients regularly",
        "Pharmacists counsel patients about medications explaining proper dosage and side effects every day carefully",
        "The taxi driver navigates city streets transporting passengers to their destinations every day safely",
        "Construction workers build structures following blueprints and safety protocols every day on various job sites",
        "The hairstylist cuts and styles hair creating new looks for clients every day at salon",
        "Postal workers sort and deliver mail to homes and businesses every day regardless of weather",
        "The paramedic responds to medical emergencies providing care to patients every day throughout the city",
        "Scientists analyze data from experiments drawing conclusions that advance knowledge every day in laboratories carefully",
        "The yoga instructor teaches poses and breathing techniques to students in classes every day patiently",
        "Electricians wire new buildings ensuring proper installation of electrical systems every day on construction sites",
        "The physical therapist helps patients recover from injuries through exercise programs every day with encouragement",
        "Social workers connect families with resources and support services every day in the community office",
        "The chef experiments with flavors creating innovative dishes for restaurant menus every day with creativity",
        "Warehouse workers organize inventory and fill orders accurately every day in the large storage facility",
        "The massage therapist relieves muscle tension using various techniques for clients every day at spa",
        "Graphic designers create visual content for websites and advertisements every day using computer software",
        "The optician helps customers select frames and adjusts eyeglasses for comfort every day at store",
        "Real estate agents show properties to potential buyers explaining features every day throughout the neighborhood",
        "The personal trainer develops workout plans helping clients achieve fitness goals every day at gym",
        "Emergency dispatchers coordinate responses directing help to those in need every day from call center",
        "The flight attendant ensures passenger safety and comfort during flights every day with professional courtesy",
        "Lab technicians conduct tests analyzing samples for medical diagnoses every day in hospital laboratories carefully",
        "The event planner organizes celebrations coordinating details to create memorable experiences every week for clients",
        "Firefighters inspect buildings checking safety equipment and identifying hazards every week in the community thoroughly",
        "The guidance counselor advises students on academic and personal matters every day in school offices",
        "Web developers create and maintain websites ensuring functionality and user experience every day at companies"
    ]
    
    all_hard_sentences = [
        # 15-25 word sentences (150 total - TRULY CHALLENGING!)
        "My absolute favorite hobby is drawing extremely colorful and detailed pictures in my special sketchbook every single evening after I finish all my homework",
        "Every Sunday evening I help my mother prepare delicious homemade dinner for our entire extended family who regularly gather at our house to celebrate being together",
        "During the wonderful summer vacation we visit interesting historical places and fascinating museums and take lots of memorable photographs to preserve those precious moments forever in albums",
        "The incredibly hardworking farmer wakes up very early every single morning to water the crops and check for any harmful pests or plant diseases in the fields",
        "My younger brother genuinely enjoys reading exciting adventure storybooks with thrilling plots before going to sleep at night in his comfortable and cozy bed with soft pillows",
        "The magnificent butterfly with beautiful colorful wings flew gracefully across our blooming garden yesterday afternoon while we watched in complete amazement and wonder at its elegant movements",
        "Professional musicians dedicate countless hours every single day to practicing their instruments and perfecting extremely complex musical compositions for their upcoming important concerts and public performances worldwide",
        "The experienced chef carefully prepares elaborate multi course meals using fresh organic ingredients sourced from local sustainable farms in the surrounding region to ensure quality and taste",
        "Ancient civilizations built magnificent pyramids and impressive temples using primitive tools and basic techniques that still absolutely amaze modern architects historians and engineers even today after thousands of years",
        "The dedicated scientist conducts meticulous laboratory experiments every day to develop new medicines and treatments that could potentially save countless precious human lives worldwide and cure diseases",
        "Talented professional athletes undergo rigorous intensive training programs for many years to compete at the highest levels in prestigious international sporting competitions and win gold medals for countries",
        "The passionate teacher explains difficult mathematical concepts using creative visual aids and interactive demonstrations to help students understand better and succeed in their academic studies and examinations",
        "Skilled artisans handcraft beautiful intricate jewelry pieces using precious metals and gemstones that have been carefully selected for their exceptional quality beauty and lasting durability over many generations",
        "The curious child asked numerous thoughtful questions about the mysterious universe and how different celestial bodies interact with each other in the vast expanse of outer space",
        "Experienced pilots must complete thousands of flight hours and pass rigorous examinations before they can fly commercial aircraft carrying hundreds of passengers safely across continents and oceans daily",
        "The talented musician composed an incredibly beautiful symphony that captured the hearts of audiences worldwide and earned numerous prestigious international awards and recognition from critics and peers everywhere",
        "Dedicated researchers work tirelessly in laboratories analyzing complex data to find solutions to some of humanity's most challenging medical problems and develop innovative treatments for serious diseases",
        "The ambitious entrepreneur developed an innovative technology platform that revolutionized the way people communicate and share information globally every day through digital networks and mobile devices worldwide",
        "Professional photographers travel to remote locations around the world to capture stunning images of rare wildlife in their natural undisturbed habitats for conservation awareness and scientific research purposes",
        "The compassionate doctor spent countless hours treating patients suffering from various illnesses and providing emotional support to their worried families during difficult times at the hospital emergency department",
        "Talented dancers spend many years perfecting their technique through daily practice sessions that strengthen their bodies and improve their artistic expression significantly for stage performances and competitions worldwide",
        "The brilliant scientist discovered a groundbreaking method for converting renewable energy sources into electricity more efficiently than ever before imagined which could help solve global energy crisis",
        "Skilled architects design magnificent buildings that combine functionality with aesthetic beauty while considering environmental sustainability and energy efficiency to minimize carbon footprint and protect our precious planet",
        "The dedicated veterinarian treats injured animals with gentle care and performs complicated surgical procedures to save the lives of beloved family pets and restore their health completely",
        "Accomplished writers spend countless hours crafting compelling stories that transport readers to different worlds and evoke powerful emotional responses through vivid descriptions and relatable characters that resonate deeply",
        "The experienced archaeologist carefully excavates ancient artifacts buried for thousands of years to better understand how our ancestors lived their daily lives and developed their unique cultures",
        "Talented artists create breathtaking paintings and sculptures that reflect their unique perspectives and interpretations of the world around them using various mediums techniques and styles throughout art history",
        "The meticulous watchmaker repairs intricate timepieces using specialized tools and demonstrates exceptional patience while working on tiny delicate components that require steady hands and excellent eyesight always",
        "Professional athletes maintain strict training schedules and follow carefully planned nutrition programs to keep their bodies in peak physical condition for competitions and achieve optimal performance results consistently",
        "The brilliant mathematician solved an extremely complex equation that had puzzled scholars and researchers for many decades using innovative mathematical approaches and creative thinking methods never tried before",
        "Dedicated teachers spend countless hours preparing engaging lesson plans and providing individualized attention to help each student reach their full potential and achieve academic success in their studies",
        "The innovative engineer designed an efficient transportation system that reduces traffic congestion and minimizes environmental impact in crowded urban areas improving quality of life for millions of city residents",
        "Skilled craftspeople create beautiful handmade furniture using traditional woodworking techniques passed down through many generations of their families maintaining cultural heritage and artistic traditions over centuries",
        "The passionate environmentalist works tirelessly to protect endangered species and preserve natural habitats threatened by human development and climate change through advocacy education and conservation efforts worldwide",
        "Talented musicians collaborate to create harmonious symphonies that blend different instruments and voices into one cohesive beautiful artistic expression that moves audiences emotionally and showcases collective creativity",
        "The experienced surgeon performs delicate operations with remarkable precision using advanced medical technology and techniques developed through years of practice training and continuous learning in the field",
        "Dedicated social workers help vulnerable individuals and families overcome difficult challenges by connecting them with essential resources and providing emotional support during crisis situations and transitions",
        "The accomplished astronomer discovers new celestial objects in deep space using powerful telescopes and sophisticated computer programs to analyze collected data from distant stars and galaxies billions of light years away",
        "Professional chefs create exquisite culinary masterpieces by combining unique ingredients with innovative cooking techniques and presenting them with artistic flair that delights all senses and creates memorable dining experiences",
        "The brilliant physicist develops groundbreaking theories about the fundamental nature of the universe using complex mathematical models and experimental evidence gathered from particle accelerators and astronomical observations worldwide",
        "Skilled translators convert important documents and literary works from one language to another while preserving the original meaning cultural context and artistic nuances that make communication across cultures possible",
        "The dedicated firefighter risks personal safety to rescue people trapped in burning buildings and provides emergency medical assistance at accident scenes showing incredible bravery and commitment to community service",
        "Talented choreographers create stunning dance performances that tell compelling stories through graceful movements and powerful emotional expressions by dancers who train rigorously to perfect every gesture and step",
        "The innovative software developer creates useful applications that solve real world problems and improve the daily lives of millions of users worldwide through intuitive interfaces and efficient code",
        "Experienced pilots navigate aircraft through challenging weather conditions using advanced instruments and rely on years of training to ensure passenger safety during flights across continents and over vast oceans",
        "The compassionate counselor helps individuals overcome personal struggles by providing guidance encouragement and practical strategies for positive life changes that lead to better mental health and wellbeing overall",
        "Accomplished poets craft beautiful verses that capture complex emotions and universal human experiences using carefully chosen words and rhythmic patterns that resonate with readers across different cultures and time periods",
        "The dedicated marine biologist studies ocean ecosystems to understand how different species interact and develops strategies to protect endangered marine life from threats like pollution and climate change worldwide",
        "Skilled electricians install and repair complex wiring systems in buildings ensuring that electrical power is distributed safely and efficiently throughout all rooms floors and areas without any hazards or malfunctions",
        "The innovative urban planner designs sustainable cities that balance residential commercial and green spaces to create livable communities for future generations while considering environmental impact and resource management",
        "Professional journalists investigate important stories thoroughly and report facts accurately to keep the public informed about significant events and issues affecting society politics economy and culture in their communities",
        "The accomplished violinist performs classical concertos with exceptional technical skill and emotional depth that moves audiences to tears during sold out concerts at prestigious venues and music festivals worldwide",
        "Dedicated paramedics provide critical emergency medical care at accident scenes and transport injured patients safely to hospitals for additional treatment working long shifts to save lives every day",
        "The talented graphic designer creates visually stunning advertisements and brand identities that effectively communicate messages and capture consumer attention in crowded marketplaces using color typography and innovative layouts",
        "Experienced mechanics diagnose and repair complex automotive problems using specialized diagnostic equipment and comprehensive knowledge of vehicle systems accumulated through years of hands on training and practical experience",
        "The passionate historian researches and documents important events from the past using primary sources to provide accurate accounts for future generations ensuring that cultural heritage and lessons are preserved",
        "Skilled carpenters construct beautiful custom furniture pieces using high quality materials and traditional woodworking techniques passed down through generations creating heirloom pieces that last for many decades",
        "The innovative biotechnology researcher develops new medical treatments using cutting edge genetic engineering techniques that could revolutionize healthcare worldwide and provide cures for previously untreatable diseases affecting millions",
        "Professional meteorologists analyze atmospheric data from multiple sources to create accurate weather forecasts that help people plan their daily activities and prepare for severe storms and natural disasters safely",
        "The dedicated librarian helps patrons locate information and resources while maintaining organized collections and creating programs to promote literacy in communities serving diverse populations with varying information needs",
        "Talented cinematographers capture stunning visual sequences using sophisticated camera equipment and lighting techniques to create memorable scenes in films that tell compelling stories and evoke strong emotional responses",
        "The experienced geologist studies rock formations and mineral deposits to understand Earth's geological history and locate valuable natural resources for extraction while considering environmental impact and sustainability issues",
        "Accomplished pianists master extremely difficult musical compositions through countless hours of dedicated practice and perform with remarkable technical precision and artistry at concerts worldwide earning critical acclaim and awards",
        "The innovative aerospace engineer designs advanced spacecraft and propulsion systems that enable humanity to explore distant planets and expand knowledge of the universe through manned and unmanned missions",
        "Professional sommeliers possess encyclopedic knowledge of wines from different regions and expertly pair beverages with cuisine to enhance dining experiences at fine restaurants creating perfect flavor combinations for guests",
        "The dedicated physical therapist helps patients recover from injuries through customized exercise programs and manual therapy techniques that restore mobility and strength allowing them to return to normal activities",
        "Skilled forensic scientists analyze evidence from crime scenes using advanced laboratory techniques and scientific methods to help solve complex criminal investigations and bring justice to victims and their families",
        "The passionate anthropologist studies human cultures and societies throughout history to understand how people adapt to different environments and develop unique traditions customs and belief systems across diverse regions",
        "Accomplished conductors lead orchestras through complex musical performances by coordinating dozens of musicians and interpreting composers' intentions with artistic vision creating unified memorable concerts that inspire audiences worldwide",
        "The innovative agricultural scientist develops new farming techniques and crop varieties that increase food production while reducing environmental impact to help feed the growing global population sustainably",
        "Professional diplomats negotiate international agreements and resolve conflicts between nations through careful diplomacy and communication skills helping to maintain peace and promote cooperation in the global community",
        "The dedicated speech therapist helps children and adults overcome communication disorders through specialized exercises and techniques that improve their ability to express themselves clearly and confidently in various situations",
        "Talented fashion designers create innovative clothing collections that reflect current trends while pushing artistic boundaries using unique fabrics colors and silhouettes that influence the global fashion industry significantly",
        "The experienced oceanographer explores deep sea environments using submersibles and remote sensing technology to discover new species and understand marine ecosystems that cover most of our planet's surface",
        "Skilled opticians craft precision eyewear and contact lenses that correct vision problems and improve quality of life for millions of people worldwide using advanced optical technology and careful measurements",
        "The passionate music teacher inspires young students to develop their musical talents through patient instruction and encouragement helping them discover the joy of creating and performing music for others",
        "Professional editors refine written content for publication ensuring clarity accuracy and proper style while working with authors to improve their work and bring important stories and information to readers",
        "The dedicated conservationist works to protect threatened ecosystems and wildlife habitats through research advocacy and hands on restoration efforts combating deforestation pollution and other environmental threats globally",
        "Talented illustrators create captivating visual art for books magazines and digital media using various techniques and styles that bring stories to life and enhance reader engagement and comprehension",
        "The innovative robotics engineer designs and programs autonomous machines that can perform complex tasks in manufacturing healthcare and exploration expanding the possibilities of human technological achievement dramatically",
        "Professional voice actors bring animated characters and narrations to life through skilled vocal performances that convey emotion personality and meaning using only their voices to create memorable entertainment experiences",
        "The experienced horticulturist cultivates and studies plants developing new varieties and techniques that improve agricultural productivity and enhance the beauty of gardens parks and landscapes in communities worldwide",
        "Skilled midwives provide essential care and support to expectant mothers during pregnancy childbirth and postpartum period combining medical knowledge with compassionate personal attention to ensure healthy outcomes",
        "The passionate art historian analyzes and interprets visual art from different periods and cultures providing context and insights that deepen understanding and appreciation of human creative expression throughout history",
        "Professional sommeliers not only recommend wines but also educate customers about grape varieties regions production methods and proper storage techniques sharing their expertise to enhance appreciation of viticulture",
        "The dedicated emergency dispatcher coordinates responses to crisis situations by gathering critical information from callers and directing appropriate resources working under pressure to help save lives every single day",
        "Talented portrait photographers capture the essence and personality of their subjects through careful composition lighting and timing creating images that preserve memories and tell stories for generations to come",
        "The innovative materials scientist develops new substances with unique properties that enable advances in technology medicine and manufacturing pushing the boundaries of what is physically possible with matter",
        "Professional landscape architects design outdoor spaces that blend functionality aesthetics and environmental sustainability creating parks gardens and public areas that enhance communities and connect people with nature beautifully",
        "The experienced trauma counselor helps individuals process and heal from difficult experiences through evidence based therapeutic techniques providing safe space and professional guidance for recovery and personal growth",
        "Skilled gemologists identify authenticate and appraise precious stones using specialized knowledge and equipment ensuring accurate valuation and helping prevent fraud in the jewelry and investment markets globally",
        "The passionate wildlife photographer documents rare and endangered species in their natural habitats through patient observation and technical skill raising awareness about conservation issues and the beauty of biodiversity",
        "Professional perfumers blend aromatic compounds to create signature fragrances that evoke emotions and memories using their highly trained sense of smell and understanding of chemistry to produce olfactory art",
        "The dedicated neuroscientist investigates the complex workings of the human brain using advanced imaging and research methods seeking to understand consciousness memory and neurological disorders for improved treatments",
        "Talented stunt coordinators design and execute dangerous action sequences in films and television ensuring performer safety while creating thrilling entertainment that captivates audiences worldwide with realistic spectacular feats",
        "The innovative industrial designer creates functional aesthetically pleasing products that improve daily life by considering user needs manufacturing processes and environmental impact in their creative design solutions",
        "Professional genealogists help people discover their family history and ancestry through careful research of historical records documents and genetic data connecting individuals to their roots and cultural heritage",
        "The experienced falconer trains and works with birds of prey using ancient techniques passed down through centuries forming unique bonds with these magnificent creatures for hunting demonstration and conservation purposes",
        "The accomplished documentary filmmaker travels extensively to capture compelling stories from diverse communities around the world presenting authentic perspectives that educate inform and inspire audiences to take meaningful action",
        "Professional sommeliers carefully curate wine selections for prestigious restaurants developing extensive knowledge of vintages regions and varietals to provide expert recommendations that enhance customers' dining experiences significantly every evening",
        "The dedicated marine conservationist works tirelessly to protect fragile coral reef ecosystems from destructive fishing practices and climate change impacts implementing sustainable solutions that preserve biodiversity for future generations worldwide",
        "Accomplished neurosurgeons perform incredibly delicate brain and spinal cord operations using advanced microsurgical techniques and state of the art imaging technology to treat complex neurological conditions and save patients' lives daily",
        "The innovative industrial designer creates functional aesthetically pleasing consumer products that improve daily life by carefully considering ergonomics manufacturing processes sustainability and user experience in every design decision made",
        "Professional cryptographers develop sophisticated encryption algorithms to secure sensitive digital communications and protect private information from unauthorized access in an increasingly interconnected world where cyber security threats constantly evolve",
        "The experienced trauma surgeon treats critically injured patients in busy emergency departments making split second life saving decisions under enormous pressure while coordinating with multidisciplinary medical teams twenty four hours every day",
        "Talented special effects artists create breathtaking visual sequences for blockbuster films using cutting edge computer graphics technology blending practical effects with digital elements to produce stunning imagery that captivates global audiences",
        "The dedicated environmental engineer designs innovative systems to treat contaminated water and air reduce industrial pollution and minimize ecological impact helping communities access clean safe resources essential for health and wellbeing",
        "Professional mountain guides lead challenging expeditions to remote peaks around the world ensuring climbers' safety through careful route planning risk assessment and expert knowledge of high altitude conditions and emergency procedures always",
        "The accomplished ornithologist studies rare bird species in their natural habitats documenting migration patterns breeding behaviors and population dynamics to support conservation efforts protecting endangered avian wildlife from extinction threats worldwide",
        "Skilled violin makers handcraft magnificent instruments using traditional techniques passed down through centuries selecting premium aged wood and applying numerous layers of varnish to produce exceptional tonal quality and beautiful aesthetics",
        "The innovative geneticist researches complex hereditary diseases analyzing DNA sequences to identify mutations and develop targeted gene therapies that could revolutionize medical treatment and improve outcomes for millions of patients globally",
        "Professional wine makers carefully oversee every stage of production from harvesting grapes at optimal ripeness through fermentation and aging processes creating distinctive vintages that reflect unique terroir and showcase exceptional craftsmanship annually",
        "The dedicated pediatric oncologist treats children battling cancer with compassionate care advanced chemotherapy protocols and innovative immunotherapy approaches while providing emotional support to worried families during extremely difficult challenging times",
        "Accomplished screenwriters craft compelling narratives for television and film developing complex characters and engaging plot lines through countless revisions and collaborations with directors and producers to create entertaining stories audiences worldwide love",
        "The experienced seismologist monitors earthquake activity using sensitive instruments and analyzes geological data to better understand tectonic plate movements providing early warning systems that protect communities from devastating natural disasters globally",
        "Professional air traffic controllers manage the safe efficient movement of aircraft through busy airspace coordinating thousands of flights daily while maintaining constant vigilance and making critical decisions that ensure passenger safety always",
        "The talented pastry chef creates exquisite desserts combining artistic presentation with exceptional flavors using premium ingredients classical French techniques and innovative modern approaches to delight restaurant guests with memorable sweet endings",
        "Dedicated humanitarian workers provide essential aid to vulnerable populations affected by wars natural disasters and poverty delivering food medical care and education in extremely challenging dangerous conditions to help communities rebuild lives",
        "The accomplished quantum physicist investigates the fundamental nature of reality at subatomic scales conducting experiments with particle accelerators to test theoretical predictions and advance human understanding of the universe's most mysterious phenomena",
        "Professional restoration experts carefully preserve and repair priceless historical artifacts and artworks using specialized techniques and materials ensuring cultural treasures survive for future generations to appreciate study and enjoy worldwide",
        "The innovative app developer creates useful mobile applications that solve real world problems streamline daily tasks and connect people across distances using intuitive user interfaces and efficient programming to enhance modern digital lifestyles",
        "Experienced avalanche forecasters assess dangerous snow conditions in mountainous regions analyzing weather patterns terrain features and snowpack stability to issue accurate warnings that prevent tragedies and save outdoor enthusiasts' lives regularly",
        "The accomplished textile designer creates beautiful original fabric patterns using color theory artistic vision and technical knowledge of weaving and printing processes producing unique materials for fashion houses and interior decorators worldwide",
        "Professional paleoclimatologists study ancient climate patterns by analyzing ice cores sediment layers and fossil records to understand long term environmental changes and provide crucial data for predicting future global warming impacts accurately",
        "The dedicated wildlife rehabilitator cares for injured orphaned animals with expertise and compassion treating medical conditions and teaching survival skills before safely releasing recovered creatures back into their natural habitats successfully",
        "Accomplished sound engineers record mix and master audio for music albums films and live performances using sophisticated equipment and trained ears to create perfectly balanced sonic experiences that move audiences emotionally worldwide",
        "The innovative biomedical engineer designs artificial organs prosthetic limbs and medical devices that restore function and improve quality of life for patients with disabilities and chronic conditions using advanced materials and technologies",
        "Professional volcano logists monitor active volcanoes around the world studying eruption patterns lava flows and seismic activity to predict dangerous events and protect nearby communities from catastrophic disasters through timely evacuations always",
        "The talented ballroom dancer performs elegant routines with incredible precision and grace having trained intensively for many years to master complex footwork timing and partnering skills for competitions and professional shows worldwide",
        "Dedicated search and rescue teams respond to emergencies in remote wilderness areas using specialized training equipment and determination to locate missing persons often working through harsh weather conditions to save lives courageously",
        "The accomplished lexicographer compiles comprehensive dictionaries by researching word origins meanings and usage patterns across different time periods and regions providing authoritative references that preserve and document evolving languages accurately",
        "Professional storm chasers track severe weather systems across vast distances documenting tornadoes hurricanes and thunderstorms with sophisticated equipment to advance meteorological science and improve forecasting capabilities that protect communities effectively",
        "The innovative prosthetics specialist designs and fits custom artificial limbs using cutting edge materials and technologies helping amputees regain mobility independence and confidence to pursue active fulfilling lives without limitations successfully",
        "Experienced ecologists study complex relationships between organisms and their environments conducting field research to understand biodiversity ecosystem health and environmental impacts informing conservation strategies that protect natural habitats worldwide",
        "The accomplished synchronized swimming team performs intricate choreographed routines with perfect timing and athleticism having practiced countless hours to achieve seamless coordination underwater and artistic expression that amazes audiences",
        "Professional antique appraisers evaluate valuable historical objects using expert knowledge of periods styles and makers to determine authentic ity and fair market value helping collectors museums and estates manage precious heirlooms responsibly",
        "The dedicated dialysis nurse provides critical care to patients with kidney failure operating complex machines monitoring vital signs and offering emotional support during lengthy treatments that sustain life and maintain health regularly",
        "Talented ice sculptors transform massive frozen blocks into breathtaking artistic creations using chainsaws chisels and creative vision to produce intricate detailed works for weddings corporate events and competitions worldwide before they melt",
        "The innovative nuclear engineer designs safer more efficient reactors and develops advanced technologies for clean energy production radioactive waste management and medical applications that benefit society while minimizing environmental risks globally",
        "Professional cave explorers venture deep underground discovering new passages geological formations and ancient ecosystems while carefully documenting findings and practicing responsible conservation to preserve these unique fragile environments for future research",
        "The accomplished horticulturist cultivates rare exotic plant species in botanical gardens using specialized knowledge of soil conditions climate requirements and propagation techniques to preserve biodiversity and educate the public about plant conservation",
        "Dedicated midwives provide compassionate care to pregnant women throughout labor and delivery using clinical expertise and emotional support to ensure safe healthy births while respecting cultural traditions and family wishes in diverse communities",
        "The talented glassblower creates stunning functional and decorative pieces by heating shaping and cooling molten glass using traditional furnaces and tools passed down through generations producing unique artwork prized by collectors worldwide",
        "Professional cryptozoologists investigate reports of undiscovered mysterious creatures conducting field research in remote locations analyzing evidence and interviewing witnesses to determine whether legendary animals might actually exist in hidden habitats",
        "The innovative tissue engineer grows replacement organs and body parts in laboratories using stem cells and scaffolding materials developing revolutionary medical treatments that could eliminate transplant waiting lists and save countless lives",
        "Experienced wilderness survival instructors teach essential skills for thriving in remote outdoor environments including fire starting shelter building food procurement and navigation using natural resources helping adventurers prepare for emergency situations confidently",
        "The accomplished falconry demonstrates the ancient art of hunting with trained birds of prey performing educational shows that showcase the incredible speed agility and intelligence of raptors while promoting wildlife conservation awareness globally"
    ]
    
    # Select sentences based on difficulty
    if actual_difficulty == "easy":
        sentence_pool = all_easy_sentences
    elif actual_difficulty == "medium":
        sentence_pool = all_medium_sentences
    else:  # hard
        sentence_pool = all_hard_sentences
    
    # CIVIC SENSE category override - replace general sentences with civic sense content
    if category == "general":
        civic_easy = [
            "Keep our city clean", "Respect all traffic lights", "Wait in proper queues", "Help elderly people always",
            "Use dustbins for waste", "Don't litter on streets", "Be polite to everyone", "Follow all safety rules",
            "Obey traffic police orders", "Save water every day", "Plant more green trees", "Keep parks very clean",
            "Respect public property always", "Don't waste any food", "Share with needy people", "Be kind to animals",
            "Cross roads very safely", "Keep toilets very clean", "Turn off unused lights", "Speak softly in public",
            "Don't honk unnecessarily loud", "Give seats to elderly", "Throw trash in bins", "Walk on proper footpaths",
            "Don't spit in public", "Keep schools very clean", "Wash hands before eating", "Cover mouth when coughing",
            "Be punctual always everywhere", "Listen to elders carefully", "Share your things nicely", "Say thank you always",
            "Hold doors for others", "Keep noise levels down", "Don't write on walls", "Feed stray animals kindly",
            "Recycle paper and plastic", "Save electricity every day", "Don't waste any water", "Keep beaches very clean",
            "Be honest always truthfully", "Help clean your neighborhood", "Don't damage public property", "Respect all traffic signals",
            "Use zebra crossings always", "Don't cut in queues", "Keep your surroundings clean", "Be respectful to everyone",
            "Follow rules of library", "Return borrowed things promptly", "Don't make loud noises", "Keep our environment clean",
            "Maintain proper hygiene always", "Don't smoke in public", "Respect others privacy always", "Be responsible with pets",
            "Clean up after yourself", "Don't waste public resources", "Help maintain cleanliness everywhere", "Conserve natural resources daily",
            "Be considerate of others", "Follow community rules properly", "Keep public transport clean", "Don't damage park benches",
            "Respect no smoking zones", "Use public facilities properly", "Don't vandalize public property", "Keep monuments very clean",
            "Follow fire safety rules", "Respect disabled parking spots", "Don't block emergency exits", "Keep sidewalks obstacle free",
            "Dispose batteries properly safely", "Don't pollute water sources", "Respect heritage sites always", "Keep hospital premises quiet",
            "Follow school discipline rules", "Don't waste government resources", "Respect public servants work", "Keep railway stations clean",
            "Follow bus stop queues", "Don't throw things outside", "Keep your neighborhood tidy", "Respect other cultures always",
            "Help lost children immediately", "Report suspicious activities quickly", "Don't encourage illegal parking", "Keep tourist spots clean",
            "Follow garden visiting rules", "Don't pluck public flowers", "Respect wildlife in parks", "Keep beaches plastic free",
            "Use reusable bags always", "Don't waste paper unnecessarily", "Follow swimming pool rules", "Keep gym equipment clean",
            "Respect library silence rules", "Don't damage borrowed books", "Follow cinema hall etiquette", "Keep public restrooms clean",
            "Don't block fire hydrants", "Respect ambulance right of way", "Follow pedestrian crossing rules", "Keep drains unclogged always",
        ]
        civic_medium = [
            "Always wait patiently in queues at public places without pushing others around unnecessarily",
            "We should use public transport regularly to reduce air pollution in our cities",
            "Keep your neighborhood clean by not throwing garbage on the streets or roads",
            "Always give your seat to elderly people or pregnant women in buses and trains",
            "Dispose of plastic waste properly in designated recycling bins to protect our environment",
            "Follow all traffic rules carefully to prevent accidents and ensure everyone safety on roads",
            "Plant trees in your locality regularly to make the environment greener and cleaner always",
            "Never write or draw on public walls or damage any government or private property",
            "Always cover your mouth when coughing or sneezing to prevent spreading germs to others",
            "Save water by turning off taps tightly and fixing leaky pipes immediately when noticed",
            "Speak softly in hospitals and libraries to maintain a peaceful environment for everyone there",
            "Help elderly people cross busy roads safely by offering them support and guidance always",
            "Keep public toilets clean by using them properly and washing hands with soap afterward",
            "Never honk unnecessarily in residential areas especially during early morning or late night hours",
            "Separate dry and wet waste at home before disposing to help with proper recycling",
            "Always wear seat belts in cars and helmets on bikes to protect yourself from injuries",
            "Report suspicious activities or unattended bags immediately to police or security personnel nearby",
            "Don't play loud music late at night that might disturb neighbors who are sleeping peacefully",
            "Always switch off lights and fans when leaving a room to save electricity and energy",
            "Respect people of all religions and cultures without any discrimination or prejudice whatsoever",
            "Keep beaches and riversides clean by taking your trash home after picnics or outings",
            "Never cut trees unnecessarily and always get permission from authorities before doing so legally",
            "Always stand in proper queues at bus stops and don't push others to get in",
            "Keep your pets on leash in public areas and clean up after them immediately always",
            "Always give way to emergency vehicles like ambulances and fire trucks on busy roads immediately",
            "Participate actively in community cleanliness drives organized in your neighborhood or locality regularly always",
            "Never waste food and always share extra food with those who desperately need it",
            "Always use zebra crossings when crossing roads and look both ways before stepping onto road",
            "Keep public gardens beautiful by not plucking flowers or damaging plants growing there naturally",
            "Always respect traffic police and cooperate with them fully when they stop you on road",
            "Never throw waste from moving vehicles onto roads as it pollutes the environment very badly",
            "Always park vehicles in designated parking areas only and not on footpaths or roads ever",
            "Keep your surroundings mosquito free by not letting water stagnate in open containers anywhere outside",
            "Always be kind to street animals and provide them food and water regularly if possible",
            "Never waste water while bathing or washing clothes and reuse water for plants when possible",
            "Always say thank you and sorry when appropriate to maintain good manners in society always",
            "Keep public transport seats clean and don't put your feet on them while traveling anywhere",
            "Always respect women and children and help them when they need assistance in public places",
            "Keep your school or college campus clean by using dustbins for throwing all waste properly",
            "Never bully or tease other children and always treat everyone with kindness and respect",
            "Always follow your school rules and regulations properly and be a responsible student every day",
            "Keep public monuments and historical places clean and don't scratch or write on them ever",
            "Never break queues at cinema halls or shopping malls even when you are in hurry",
            "Always carry a cloth bag when going shopping to reduce use of plastic bags everywhere",
            "Keep hospital premises quiet and peaceful for patients who are recovering from illnesses there now",
            "Never smoke in public places or near children as secondhand smoke is very harmful to health",
            "Always be punctual for all appointments and meetings to show respect for other people time",
            "Never throw stones at birds or animals as this causes them great pain and suffering",
            "Always respect differently abled people and help them navigate public spaces when they need help",
            "Keep your workplace clean and organized to maintain a pleasant environment for everyone working there",
        ]
        civic_hard = [
            "Every responsible citizen should actively participate in maintaining cleanliness of public spaces by properly disposing waste in designated bins and encouraging others to do the same through personal example and community awareness programs",
            "We must understand that traffic rules are designed not just to avoid penalties but to ensure safety of all road users including pedestrians cyclists motorcyclists and drivers and should be followed diligently even when traffic police are not present nearby",
            "Conservation of water resources is extremely critical in urban areas where demand exceeds supply therefore we should fix leaking taps immediately harvest rainwater install water efficient fixtures and avoid wasteful practices like washing vehicles with running water or leaving taps open",
            "Environmental protection requires collective effort from all citizens who should actively reduce plastic usage by carrying reusable bags refusing single use plastics properly segregating waste at source and participating in tree plantation drives to combat climate change effects in our communities",
            "Respecting elderly citizens is not merely a cultural tradition but a fundamental civic duty that includes offering seats in public transport helping them cross busy streets speaking to them politely and ensuring they receive proper care attention and dignity in all social interactions",
            "Queue discipline at public places like bus stops railway stations cinema halls and government offices reflects the maturity of society and citizens should patiently wait their turn without pushing cutting lines or using influence to get preferential treatment at any counter",
            "Public property including parks gardens monuments government buildings and infrastructure is built using taxpayers money and citizens have moral responsibility to maintain it properly report damage immediately and discourage vandalism through community vigilance and social awareness among all age groups",
            "Noise pollution in residential areas especially during night hours seriously affects health and wellbeing of residents particularly students elderly and sick people therefore we should avoid loud music honking unnecessarily and using loudspeakers without proper permission from appropriate local authorities",
            "Energy conservation is crucial for sustainable development and every household should contribute by using LED bulbs switching off unnecessary lights and fans using energy efficient appliances optimizing air conditioner usage and exploring renewable energy options like solar panels rooftop installations",
            "Healthcare facilities must be kept clean quiet and hygienic to ensure proper recovery of patients therefore visitors should follow hospital rules speak softly maintain cleanliness dispose medical waste properly and cooperate with healthcare workers who work tirelessly for patient welfare daily",
            "Road safety requires conscious effort from all stakeholders including strictly following speed limits using seat belts and helmets avoiding drunk driving not using mobile phones while driving and always giving right of way to emergency vehicles ambulances and fire trucks on roads",
            "Community participation in neighborhood cleanliness drives tree plantation campaigns awareness programs and social welfare activities strengthens social bonds promotes civic sense and creates a better living environment for present and future generations to enjoy prosper and be proud of",
            "Recycling and proper waste management at household level significantly reduces burden on municipal systems and environmental impact therefore citizens should segregate wet and dry waste compost organic materials and ensure proper disposal of hazardous materials like batteries and electronic waste carefully",
            "Respect for cultural diversity religious tolerance and social harmony are cornerstone values of civilized society and citizens should actively oppose discrimination prejudice and hatred based on religion caste gender or economic status through education awareness and exemplary personal conduct daily",
            "Electoral participation is fundamental right and civic duty of every eligible citizen who should register to vote study candidates thoroughly participate in democratic process and cast informed vote without succumbing to money power caste considerations or false promises made by politicians",
            "Public transport systems serve millions of commuters daily and passengers should maintain cleanliness offer seats to elderly pregnant women and disabled persons follow safety protocols and treat fellow passengers and transport staff with genuine courtesy and respect at all times always",
            "Wildlife conservation and protection of natural habitats require conscious efforts from citizens who should not encroach forest areas respect wildlife corridors report poaching incidents support conservation initiatives and educate next generation about importance of biodiversity and ecological balance for future",
            "Corruption undermines development and public trust therefore citizens should refuse to pay bribes report corrupt officials demand transparency in government functioning and support anti corruption measures through legal channels and social media advocacy campaigns for meaningful and lasting systemic change",
            "Disaster preparedness at individual and community level can save countless lives during emergencies therefore citizens should keep emergency supplies ready learn basic first aid participate in mock drills and help vulnerable neighbors during natural calamities like floods earthquakes or cyclones",
            "Digital citizenship in modern era requires responsible use of internet and social media avoiding spread of fake news respecting others privacy protecting personal information from cyber criminals and reporting illegal activities like hate speech child exploitation or financial frauds to authorities",
        ]
        if actual_difficulty == "easy":
            sentence_pool = civic_easy
        elif actual_difficulty == "medium":
            sentence_pool = civic_medium
        else:
            sentence_pool = civic_hard

    elif category == "animals":
        animals_easy = [
            "The dog runs fast", "A cat drinks milk", "Birds fly very high", "Fish swim in rivers",
            "The cow gives milk", "Elephants are very big", "Rabbits are quite small", "Lions are very fierce",
            "Monkeys climb tall trees", "Frogs jump into ponds", "Bees make sweet honey", "Butterflies look very pretty",
            "Horses run really fast", "Ducks swim in water", "Snakes move without legs", "Owls see in dark",
            "Parrots talk like humans", "Tigers are very strong", "Giraffes have long necks", "Zebras have black stripes",
            "Puppies love to play", "Kittens are very soft", "Baby ducks are yellow", "The bear is huge",
            "Deer have pretty antlers", "Peacocks have beautiful feathers", "Eagles fly really high", "Wolves live in packs",
            "Penguins cannot fly high", "Dolphins are very smart", "Whales are the biggest", "Ants are very tiny",
            "Spiders spin their webs", "Crabs walk quite sideways", "Turtles move very slowly", "Foxes are very clever",
            "Camels live in deserts", "Polar bears love ice", "Kangaroos carry their babies", "Octopus has eight arms",
        ]
        animals_medium = [
            "The elephant is the largest land animal in the world and lives in Africa and Asia",
            "Dolphins are very intelligent creatures that communicate with each other through different kinds of sounds",
            "A mother hen takes very good care of all her little baby chicks every day",
            "The beautiful butterfly starts its life as a small caterpillar before growing wings and flying",
            "Wild tigers are endangered animals that need our help and protection to survive in nature",
            "Penguins are amazing birds that cannot fly but they are expert swimmers in cold water",
            "Bees are very important insects because they help flowers grow by carrying pollen from one to another",
            "The giraffe can reach leaves on very tall trees because of its extraordinarily long neck",
            "Dogs have been loyal companions to humans for thousands of years and love to please their owners",
            "Migratory birds travel thousands of kilometers every year to warmer places when winter arrives in their home",
            "Crocodiles are ancient reptiles that have lived on earth for millions of years without much change",
            "The cheetah is the fastest land animal and can run at speeds of over one hundred kilometers",
            "Whales are not fish but mammals that breathe air and feed their babies with their own milk",
            "Honey bees live in colonies with one queen bee and thousands of worker bees in their hive",
            "The parrot is one of the few birds that can mimic human speech and learn many words",
            "Wolves are social animals that live and hunt in groups called packs led by an alpha pair",
            "Sea turtles travel thousands of kilometers across oceans to lay their eggs on the same beach where they were born",
            "Camels can survive for many days without water because they store fat in their large humps",
            "Baby kangaroos called joeys live in their mother's pouch and drink milk for the first months of life",
            "The octopus is a very intelligent sea creature that can change its color and shape to hide from predators",
        ]
        animals_hard = [
            "Migration is a remarkable natural phenomenon where millions of animals travel enormous distances across continents following seasonal changes in weather and food availability guided by magnetic fields stars and learned routes passed down through generations",
            "The ecosystem maintains a delicate balance where predators and prey exist in harmony and removing any single species from the food chain can trigger cascading effects that dramatically alter the entire habitat and affect countless other organisms",
            "Zoologists who dedicate their careers to studying animal behavior have discovered that many species demonstrate remarkable cognitive abilities including problem solving tool use social learning and even basic forms of communication that challenge our understanding of intelligence",
            "Conservation biology focuses on protecting endangered species from extinction by studying their habitat requirements breeding patterns population dynamics and the specific human activities that threaten their survival while developing effective strategies for their long term protection",
            "The complex social structures observed in elephant herds where matriarchs lead multigenerational family groups share accumulated knowledge about water sources safe migration routes and appropriate responses to threats demonstrate sophisticated memory and communication abilities rarely seen in mammals",
            "Marine biologists studying coral reef ecosystems have documented extraordinary relationships between different species including cleaner fish that remove parasites from larger fish creating mutually beneficial partnerships that have evolved over millions of years of coexistence in these rich underwater habitats",
        ]
        if actual_difficulty == "easy":
            sentence_pool = animals_easy
        elif actual_difficulty == "medium":
            sentence_pool = animals_medium
        else:
            sentence_pool = animals_hard

    elif category == "food":
        food_easy = [
            "Rice is very tasty", "Apples are quite sweet", "Milk is very cold", "Bread is so soft",
            "Eggs are very healthy", "Bananas give us energy", "Fish is very fresh", "Mangoes taste so sweet",
            "Vegetables are very healthy", "Soup is quite warm", "Ice cream is cold", "Chocolate is very sweet",
            "Pizza is very hot", "Oranges are very juicy", "Grapes are quite small", "Watermelon is so big",
            "Curd is very smooth", "Juice is quite fresh", "Cake is very yummy", "Biscuits are so crispy",
            "Carrots are quite crunchy", "Tomatoes are bright red", "Potatoes can be fried", "Onions make us cry",
            "Honey is very sweet", "Salt makes food tasty", "Sugar is quite sweet", "Pepper is very spicy",
            "Butter is very soft", "Cheese is quite yummy", "Noodles are very long", "Roti is quite round",
            "Dosa is very crispy", "Idli is very soft", "Sambar is quite spicy", "Chutney is very yummy",
            "Poha is very light", "Upma is quite warm", "Paratha is very filling", "Biryani smells so good",
        ]
        food_medium = [
            "Eating fresh fruits and vegetables every day keeps our body strong and our mind very sharp",
            "My mother makes delicious homemade food every single day for our entire family with great love",
            "We should always wash our hands thoroughly with soap before eating any food for good hygiene",
            "A balanced diet includes proteins carbohydrates vitamins minerals and enough water to keep us all healthy",
            "Street food in India is very popular and comes in hundreds of varieties and delicious flavors",
            "Cooking is an important life skill that everyone should learn to take care of themselves properly",
            "Traditional Indian food uses many spices like turmeric coriander cumin and cardamom that are very healthy",
            "Junk food like chips and soft drinks should be eaten only occasionally as they harm our health",
            "Breakfast is the most important meal of the day as it gives us energy for all activities",
            "Farmers work very hard throughout the year to grow fresh vegetables and grains for all of us",
            "Different states in India have their own unique food traditions and special dishes that are very famous",
            "Fermented foods like yogurt and idli batter are very nutritious and good for our digestive health",
            "Children need extra calcium from milk and dairy products every day for their growing bones and teeth",
            "Food preservation techniques like pickling drying and refrigeration help us store food for much longer periods",
            "Organic farming without chemical pesticides produces healthier food that is better for both humans and environment",
            "Many traditional foods have great medicinal properties and our grandmothers knew exactly how to use them",
            "Water is the most essential nutrient and we must drink at least eight glasses of water every day",
            "Sharing food with family and friends during festivals and celebrations is a beautiful tradition in all cultures",
            "Learning to read food labels helps us understand what we are eating and make healthier food choices",
            "Reducing food waste at home by planning meals carefully and storing leftovers properly saves money and resources",
        ]
        food_hard = [
            "Nutrition science has established that a varied diet rich in whole foods including diverse fruits vegetables legumes whole grains lean proteins and healthy fats provides all the micronutrients and macronutrients essential for optimal physical growth cognitive development and long term disease prevention",
            "The globalization of food culture has created both opportunities and challenges as traditional dietary patterns that evolved over centuries to suit local climates and body types are increasingly being displaced by processed foods high in refined sugars unhealthy fats and artificial additives",
            "Food security is a pressing global challenge where millions of people lack reliable access to sufficient nutritious food while simultaneously vast quantities of edible food are wasted at every stage of the supply chain from farm to consumer requiring comprehensive policy solutions",
            "The relationship between gut microbiome diversity and overall health has emerged as a fascinating area of research suggesting that the trillions of microorganisms living in our digestive system profoundly influence immunity mental health metabolism and even our susceptibility to various chronic diseases",
            "Traditional culinary practices developed by indigenous communities over thousands of years often demonstrate sophisticated understanding of food combinations fermentation techniques and medicinal properties of plants that modern nutritional science is only beginning to validate through systematic research and clinical studies",
        ]
        if actual_difficulty == "easy":
            sentence_pool = food_easy
        elif actual_difficulty == "medium":
            sentence_pool = food_medium
        else:
            sentence_pool = food_hard

    elif category == "sports":
        sports_easy = [
            "Cricket is very popular", "Football is quite fun", "Swimming is very healthy", "Running builds good fitness",
            "Badminton is quite fast", "Tennis is very exciting", "Basketball is really tall", "Volleyball is quite fun",
            "Chess requires deep thinking", "Kabaddi is very fast", "Hockey uses long sticks", "Baseball uses round bats",
            "Players train very hard", "Teams play together well", "Goals score match points", "Winners get shiny medals",
            "Coaches train the players", "Referees enforce the rules", "Stadiums hold many fans", "Fans cheer very loud",
            "Athletes run very fast", "Swimmers dive quite deep", "Jumpers leap very high", "Throwers throw quite far",
            "Sports build our strength", "Games teach us teamwork", "Exercise keeps us fit", "Practice makes us perfect",
            "Balls come in different sizes", "Bats come in different shapes", "Nets separate the sides", "Goals define the target",
            "Uniforms identify the teams", "Whistles signal the starts", "Trophies reward the winners", "Medals honor the champions",
            "Olympics unite all nations", "World cups gather best teams", "Records are broken often", "Legends inspire young players",
        ]
        sports_medium = [
            "Cricket is the most popular sport in India and millions of fans watch every important match on television",
            "Regular physical exercise through sports keeps our body fit and our mind fresh and full of energy",
            "Team sports like football and basketball teach us the very important values of cooperation trust and communication",
            "Olympic Games bring together athletes from all countries of the world to compete in friendly competition",
            "A good sports coach motivates players trains them technically and helps them develop strong mental strength",
            "Playing outdoor sports with friends is much more beneficial for children than sitting indoors watching screens all day",
            "Professional athletes follow very strict diet and training schedules to maintain their peak physical performance year round",
            "Swimming is an excellent full body workout that is gentle on joints and suitable for all age groups",
            "Chess is a board game that develops strategic thinking concentration memory and problem solving abilities in players",
            "India has produced many legendary sports champions in cricket hockey wrestling boxing and athletics who inspire youth",
            "Sports teach children valuable life lessons like discipline perseverance sportsmanship dealing with defeat and celebrating victory",
            "Many schools organize annual sports days where students participate in various track field and team sports events",
            "Kabaddi is a traditional Indian contact sport that requires exceptional breath control agility and physical strength",
            "Adequate rest and recovery is just as important as training for athletes to avoid injuries and perform well",
            "Badminton is one of the fastest racket sports in the world with shuttlecocks traveling at incredible speeds",
            "Physical education classes in school should be taken seriously as they develop fitness habits for the entire life",
            "Young athletes need proper nutrition sufficient sleep good coaching and consistent practice to reach their full potential",
            "Yoga and meditation are increasingly being incorporated into training programs of elite athletes to improve focus and recovery",
            "Water sports like kayaking rowing and surfing combine physical fitness with appreciation for natural aquatic environments",
            "The spirit of sportsmanship means competing fairly respecting opponents following rules and accepting results with grace",
        ]
        sports_hard = [
            "The psychology of peak athletic performance encompasses complex interactions between physical conditioning mental resilience tactical awareness and the ability to perform under intense pressure which is why modern sports teams invest heavily in sports psychologists alongside traditional coaches and fitness trainers",
            "Sports medicine has advanced dramatically enabling athletes to recover from injuries that would have permanently ended careers just decades ago through innovative surgical techniques rehabilitation protocols nutritional interventions and cutting edge technologies like cryotherapy electromagnetic stimulation and biomechanical analysis",
            "The economics of professional sports has transformed into a multi-billion dollar global industry where elite athletes earn extraordinary sums through performance contracts endorsements media rights and commercial partnerships while grassroots sports often struggle for adequate funding and facilities particularly in developing nations",
            "Doping and performance enhancement substances remain serious ethical challenges in competitive sports undermining fair competition damaging athlete health and eroding public trust while anti-doping agencies continuously develop more sophisticated detection methods to maintain the integrity of sporting competitions worldwide",
            "Physical education research demonstrates that regular participation in structured sports activities during childhood and adolescence significantly improves academic performance enhances social development builds emotional resilience and establishes healthy lifestyle habits that persist throughout adulthood reducing chronic disease risk considerably",
        ]
        if actual_difficulty == "easy":
            sentence_pool = sports_easy
        elif actual_difficulty == "medium":
            sentence_pool = sports_medium
        else:
            sentence_pool = sports_hard

    elif category == "feelings":
        feelings_easy = [
            "I feel very happy", "She is quite sad", "He is very angry", "They are quite scared",
            "We feel very excited", "I am very proud", "She looks quite tired", "He feels quite sick",
            "They are very bored", "We feel very calm", "I am very nervous", "She is very shy",
            "He is quite lonely", "They are very grateful", "We feel very loved", "I feel quite hurt",
            "She is very worried", "He feels quite confused", "They are very surprised", "We feel very safe",
            "Smiling shows our happiness", "Crying shows our sadness", "Laughing means real joy", "Hugging shows our care",
            "Kindness makes us happy", "Anger hurts everyone badly", "Fear makes us tremble", "Courage makes us brave",
            "Love is very beautiful", "Hate is quite harmful", "Hope is very powerful", "Trust builds real friendship",
            "Jealousy is quite harmful", "Patience is very useful", "Gratitude is very important", "Empathy helps us connect",
            "Forgiveness brings inner peace", "Compassion helps other people", "Joy is quite infectious", "Peace feels so wonderful",
        ]
        feelings_medium = [
            "Feeling happy and positive every day helps us do our best work and enjoy life completely",
            "When we feel angry it is very important to breathe deeply and calm down before speaking",
            "Being kind to others when they are sad or struggling makes both of us feel much better",
            "Expressing our feelings honestly but gently helps others understand us better and builds stronger relationships",
            "It is perfectly normal to feel nervous before an important exam or performance as everyone experiences this",
            "Gratitude means appreciating all the good things in our life and thanking people who help us",
            "When we help someone who is feeling lonely or upset we experience a warm feeling of satisfaction",
            "Children who learn to manage their emotions grow up to be healthier and more successful adults",
            "Talking about our feelings with a trusted person like a parent or teacher helps us feel better",
            "Being brave does not mean having no fear but it means doing what is right despite feeling afraid",
            "Jealousy is a natural feeling but acting on it by hurting others is never acceptable behavior",
            "Forgiveness is a powerful gift we give ourselves because holding grudges only hurts the person who holds them",
            "Empathy means trying to understand how another person feels which helps us treat everyone with more kindness",
            "Celebrating our achievements with genuine happiness while remaining humble about our success is a sign of maturity",
            "Loneliness is a difficult feeling that many people experience and we should always check on friends who seem isolated",
            "Meditation and mindfulness exercises help us observe our thoughts and feelings without being overwhelmed by them",
            "The feeling of accomplishment after working very hard on a difficult task is one of the best feelings",
            "Anxiety about the future is very common but focusing on what we can control helps manage these feelings",
            "Sharing joyful experiences with people we love multiplies the happiness while sharing difficult feelings reduces their burden",
            "Developing emotional intelligence helps us understand our own feelings and respond to the feelings of others wisely",
        ]
        feelings_hard = [
            "Emotional intelligence which encompasses the abilities to accurately recognize manage and effectively communicate one's own emotions while simultaneously understanding and appropriately responding to the emotions of others has been identified as a stronger predictor of life success and relationship quality than traditional measures of intelligence",
            "The neuroscience of emotions reveals that feelings are not merely psychological experiences but are deeply rooted in complex physiological processes involving the amygdala prefrontal cortex and autonomic nervous system creating bidirectional relationships between our thoughts bodily sensations and emotional states that influence all aspects of behavior",
            "Childhood experiences of emotional validation or invalidation profoundly shape the developing brain's emotional regulation systems affecting how individuals process stress form attachments make decisions and maintain mental health throughout their entire lives demonstrating the critical importance of emotionally attuned parenting and education",
            "The cultural dimensions of emotional expression vary dramatically across societies with some cultures encouraging open display of feelings while others value stoic restraint creating potential for significant misunderstandings in cross-cultural interactions and highlighting how social norms profoundly shape what emotions are considered appropriate to express",
            "Developing resilience which is the capacity to adapt positively and maintain psychological well-being despite significant adversity trauma or stress requires building strong social connections developing problem-solving skills maintaining optimistic perspectives finding meaning in difficult experiences and practicing evidence-based stress management techniques",
        ]
        if actual_difficulty == "easy":
            sentence_pool = feelings_easy
        elif actual_difficulty == "medium":
            sentence_pool = feelings_medium
        else:
            sentence_pool = feelings_hard

    elif category == "colors":
        colors_easy = [
            "The sky is blue", "Grass is very green", "Sun is bright yellow", "Roses are bright red",
            "Snow is pure white", "Night is quite dark", "Clouds are soft white", "Sea is deep blue",
            "Oranges are bright orange", "Grapes are deep purple", "Bananas are quite yellow", "Apples are bright red",
            "Leaves change to orange", "Peacocks have blue feathers", "Flamingos are quite pink", "Elephants are grey colored",
            "Coal is very black", "Gold looks very shiny", "Silver looks quite bright", "Brown is earthy warm",
            "Rainbows have seven colors", "Sunsets look orange pink", "Flowers come in many colors", "Butterflies are very colorful",
            "Red means stop here", "Green means go now", "Yellow means slow down", "Blue means so calm",
            "Pink is very soft", "Purple is quite royal", "Turquoise is quite unique", "Maroon is quite deep",
            "Cream is very light", "Beige is quite neutral", "Coral is quite warm", "Teal is very cool",
            "Indigo is very deep", "Violet is quite light", "Magenta is quite bright", "Cyan is very fresh",
        ]
        colors_medium = [
            "The rainbow appears after rain when sunlight passes through tiny water droplets and splits into seven beautiful colors",
            "Artists mix primary colors red blue and yellow together to create all the other beautiful colors they need",
            "Colors affect our mood and feelings as blue is calming red is energetic and yellow makes us happy",
            "Traffic lights use three specific colors red for stop yellow for slow down and green for go",
            "The changing colors of autumn leaves from green to orange red and brown are one of nature's most beautiful sights",
            "Different colors in nature often serve important purposes like warning predators or attracting pollinators to flowers",
            "India is a country full of vibrant colors visible in its festivals clothes art rangoli and markets",
            "The human eye can distinguish millions of different colors but we name only a small fraction of them",
            "Chameleons are fascinating reptiles that change their skin color to communicate with others and regulate body temperature",
            "Color blindness affects some people making it difficult for them to distinguish between certain colors especially red and green",
            "Interior designers use colors strategically to create different moods in rooms warm colors create energy cool colors create calm",
            "The night sky shows us beautiful colors from deep black to purple blue with stars that appear white yellow and red",
            "Photography and painting use understanding of light and color to capture beautiful moments and express creative ideas",
            "Traditional Indian clothing celebrates color with bright saris and kurtas in every imaginable shade and combination possible",
            "Mixing paints and understanding complementary and contrasting colors is a fundamental skill that all artists must develop",
            "The color of food often gives us important information about its freshness ripeness nutritional content and whether it is safe to eat",
            "Colorful coral reefs represent some of the most biodiverse ecosystems on earth but are threatened by warming oceans",
            "Color psychology is used extensively in advertising packaging and branding to influence consumer emotions and purchasing decisions",
            "Holi the festival of colors celebrates the arrival of spring and the victory of good over evil with joyful color throwing",
            "Scientists use special instruments called spectroscopes to analyze the colors in light and determine the composition of distant stars",
        ]
        colors_hard = [
            "The physics of color perception involves complex interactions between electromagnetic radiation of different wavelengths specialized photoreceptors in the retina called cones and sophisticated neural processing in the visual cortex that ultimately creates our subjective experience of seeing different hues in the visible spectrum",
            "Color symbolism varies profoundly across cultures with white representing purity and mourning in different societies red signifying danger love luck or celebration depending on cultural context demonstrating how the meaning we assign to colors is socially constructed rather than inherent to the colors themselves",
            "The evolution of color vision in primates is believed to have provided significant survival advantages in identifying ripe fruits distinguishing edible from poisonous plants and reading subtle emotional cues in conspecifics through changes in facial coloration driven by blood flow modulation",
            "Biomimicry research examining how organisms like cephalopods achieve remarkable color change through chromatophores iridophores and leucophores has inspired development of new materials for adaptive camouflage flexible displays and responsive architectural elements that could revolutionize multiple industries and technologies",
            "Color in art history reveals fascinating insights into cultural values technological capabilities and trading networks as expensive pigments like ultramarine made from lapis lazuli or vermilion from mercury sulfide were reserved for the most important subjects in paintings indicating how material constraints shaped artistic choices across civilizations",
        ]
        if actual_difficulty == "easy":
            sentence_pool = colors_easy
        elif actual_difficulty == "medium":
            sentence_pool = colors_medium
        else:
            sentence_pool = colors_hard

    elif category == "family":
        family_easy = [
            "My mother loves me", "My father works hard", "Sister helps me always", "Brother plays with me",
            "Grandmother tells good stories", "Grandfather is very wise", "Family stays together always", "Home is very safe",
            "We eat meals together", "Parents teach us well", "Children make us happy", "Cousins are quite fun",
            "Uncle visits us often", "Aunt brings sweet gifts", "Family prays together daily", "Love holds us together",
            "Mother cooks very well", "Father earns our living", "Siblings share our rooms", "Grandparents give us wisdom",
            "We celebrate together always", "Family supports us always", "Home is most comfortable", "Parents sacrifice for children",
            "Children respect their elders", "Elders guide the young", "Sisters are best friends", "Brothers protect each other",
            "Grandparents tell old stories", "Family photos show memories", "Birthdays bring whole family", "Festivals unite the family",
            "Pets join our family", "Neighbors are like family", "Friends become like family", "Love makes us family",
            "Family means everything important", "Together we are stronger", "Caring for each other matters", "Family is pure gold",
        ]
        family_medium = [
            "Families come in many different forms but what matters most is the love and care members show each other",
            "Grandparents are a precious treasure of family wisdom stories and unconditional love that children should appreciate deeply",
            "Helping with household chores teaches children responsibility and shows respect for the hard work of parents",
            "Family meals are important opportunities for everyone to reconnect share their day's experiences and strengthen relationships",
            "Siblings may sometimes quarrel but they are also our first friends protectors and lifelong companions in life",
            "Parents make enormous sacrifices of time money energy and personal dreams to give their children good opportunities",
            "Extended family gatherings during festivals and special occasions create beautiful memories that we treasure throughout our lives",
            "Communication and mutual respect are the most important foundations of a healthy and happy family relationship",
            "When a family member faces illness difficulty or grief the whole family comes together to provide support and care",
            "Teaching children good values like honesty kindness hard work and compassion is the most important duty of parents",
            "Single parent families where one parent raises children alone deserve enormous respect as this is an extremely challenging role",
            "Adopted children are loved just as deeply as biological children because family bonds are built on love not just blood",
            "Learning about our family history and ancestral roots helps us understand our identity and feel connected to our heritage",
            "Financial challenges can put great stress on family relationships but strong communication and mutual support help overcome difficulties",
            "The tradition of caring for elderly parents at home reflects beautiful values of gratitude respect and family responsibility",
            "Working mothers balance professional careers with family responsibilities demonstrating remarkable strength dedication and time management skills",
            "Family rules and boundaries when explained with love help children feel secure and learn important social behaviors",
            "Celebrating each family member's achievements however small with genuine enthusiasm builds confidence and strengthens family bonds",
            "Technology should enhance family connections across distances but not replace the irreplaceable quality of in-person family time",
            "Every family has its unique culture traditions languages foods celebrations and ways of showing love that make it special",
        ]
        family_hard = [
            "Contemporary family structures have evolved considerably from traditional nuclear family models with single parent households blended families same sex parent families multigenerational households and chosen families all representing valid relationship configurations that provide love support and belonging to their members",
            "Developmental psychology research consistently demonstrates that children raised in nurturing secure and emotionally responsive family environments develop stronger cognitive abilities better emotional regulation more positive social skills and greater resilience against adversity compared to those experiencing inconsistent or neglectful caregiving",
            "The intergenerational transmission of trauma where unresolved psychological wounds from parents or grandparents are inadvertently passed to subsequent generations through parenting practices emotional patterns and relationship models represents a significant public health concern that can be addressed through awareness therapy and conscious parenting",
            "Work-life balance has become an increasingly critical issue for modern families as economic pressures require both parents to maintain demanding careers while simultaneously trying to provide sufficient time attention and emotional presence for children's healthy development creating systemic tensions requiring both individual and policy solutions",
            "Cultural variations in family structure roles and obligations reflect diverse philosophical traditions with collectivist cultures emphasizing family loyalty interdependence and filial piety while individualist cultures prioritize personal autonomy self-actualization and nuclear family privacy creating fundamentally different understandings of family duty and individual rights",
        ]
        if actual_difficulty == "easy":
            sentence_pool = family_easy
        elif actual_difficulty == "medium":
            sentence_pool = family_medium
        else:
            sentence_pool = family_hard

    elif category == "school":
        school_easy = [
            "I love my school", "Teachers are very kind", "Books help us learn", "Classrooms are quite clean",
            "Friends study together always", "We raise our hands", "Bells ring every hour", "Lunch breaks are fun",
            "Homework is done daily", "Exams test our knowledge", "Libraries have many books", "Labs are quite interesting",
            "Sports day is exciting", "Annual day is special", "Assembly starts our day", "Prayers keep us calm",
            "Pencils write our words", "Erasers fix our mistakes", "Rulers measure straight lines", "Sharpeners sharpen pencils",
            "Bags carry our books", "Water bottles keep us hydrated", "Lunch boxes bring food", "Uniforms keep us equal",
            "Benches are quite hard", "Blackboards are quite dark", "Chalk writes on boards", "Dusters clean the boards",
            "Corridors connect our classrooms", "Playgrounds are quite large", "Toilets must stay clean", "Canteens sell our food",
            "Art classes are creative", "Music classes are melodious", "Dance classes are energetic", "Science labs are fascinating",
            "Math teaches us calculation", "English helps us communicate", "History tells old stories", "Geography shows our world",
        ]
        school_medium = [
            "Going to school every day is the best investment we can make for our own bright future",
            "A good teacher can change a student's life by inspiring them to believe in their own potential",
            "Reading books regularly improves our vocabulary knowledge imagination and ability to express our thoughts clearly",
            "Group projects teach students how to collaborate communicate divide work fairly and respect different opinions and ideas",
            "Science experiments in the laboratory make abstract concepts real and develop our curiosity and problem solving skills",
            "Students who maintain proper attendance and give full attention in class consistently perform better in examinations",
            "The school library is a treasure house of knowledge and students should make full use of this resource",
            "Physical education classes are equally important as academic subjects for developing healthy bodies and teamwork skills",
            "Taking neat organized notes during class helps students understand lessons better and review efficiently before examinations",
            "Bullying in any form is completely unacceptable and every student has the right to feel safe at school",
            "Teachers spend many hours preparing lessons correcting homework and guiding students and deserve our utmost respect",
            "Learning a second language in school opens enormous opportunities for communication travel higher education and career advancement",
            "School disciplinary rules exist not to restrict freedom but to create an environment where everyone can learn well",
            "Extracurricular activities like debate drama music and sports develop talents and skills not taught in regular classrooms",
            "Digital literacy skills including safe internet use critical evaluation of online information and responsible social media behavior are essential today",
            "Mathematics teaches logical thinking systematic problem solving and precise communication skills that are valuable in every career",
            "Inclusive education where children with different abilities learn together in the same classroom benefits all students significantly",
            "Parent involvement in children's education through attending school events monitoring homework and communicating with teachers greatly improves outcomes",
            "Career guidance and exposure to different professions in school helps students make informed choices about their future education",
            "Schools that create safe supportive environments where students feel valued respected and heard achieve the best academic and personal development outcomes",
        ]
        school_hard = [
            "Educational neuroscience research has transformed our understanding of how the brain acquires retains and applies knowledge demonstrating that active learning strategies spaced repetition interleaving of topics and retrieval practice are far more effective than passive reading and massed studying for developing durable long term memory",
            "The digital transformation of education presents both extraordinary opportunities to personalize learning provide access to world-class instruction globally and develop twenty-first century skills and significant challenges including digital divides between privileged and disadvantaged students concerns about screen time and social media's impact on adolescent development and attention",
            "Critical pedagogy as developed by educators like Paulo Freire argues that education should not merely transmit existing knowledge but should empower students to question assumptions challenge injustices understand power dynamics and become active agents of positive social transformation rather than passive recipients of predetermined curriculum",
            "Inclusive education philosophy and practice requires schools to redesign their physical environments curriculum delivery assessment methods and support systems to genuinely accommodate diverse learners including students with physical sensory cognitive emotional and social differences ensuring equal access to quality education as a fundamental right",
            "The evidence base for growth mindset interventions in educational settings suggests that teaching students to understand that intelligence is malleable rather than fixed that effort leads to improvement and that mistakes are valuable learning opportunities can significantly improve academic persistence motivation and achievement particularly among students from disadvantaged backgrounds",
        ]
        if actual_difficulty == "easy":
            sentence_pool = school_easy
        elif actual_difficulty == "medium":
            sentence_pool = school_medium
        else:
            sentence_pool = school_hard
    
    # Return a random sentence from the pool
    import random
    return random.choice(sentence_pool)

def generate_spell_word(difficulty="easy", user_level=1):
    """Generate words with variety - respects user's difficulty choice"""
    
    # Use the difficulty selected by the user directly
    actual_difficulty = difficulty
    
    # SPELL BEE WORD POOLS: 150 words per difficulty level (Alphabetical Order)
    word_pools = {
        "easy": [
            # 150 Easy words - Alphabetical Order
            "baby", "bag", "ball", "bed", "bell", "bird", "blanket", "blue", "boat", "book",
            "box", "bread", "brush", "bus", "cake", "cap", "car", "chair", "city", "class",
            "clean", "clock", "cloud", "coat", "cold", "comb", "cup", "dark", "day", "desk",
            "dog", "door", "drink", "ear", "east", "eat", "egg", "eye", "fan", "farm",
            "fast", "fish", "flower", "foot", "fork", "friend", "fruit", "garden", "glass", "grass",
            "green", "hand", "happy", "hat", "hill", "home", "hot", "jump", "key", "king",
            "knife", "lake", "laugh", "leaf", "light", "lock", "map", "milk", "mirror", "month",
            "moon", "night", "north", "nose", "park", "pen", "phone", "photo", "pink", "pillow",
            "plane", "plate", "play", "queen", "rain", "raincoat", "read", "red", "rice", "ring",
            "river", "road", "run", "sad", "salt", "sand", "school", "sea", "seed", "shirt",
            "ship", "shoe", "shop", "short", "sing", "sky", "slow", "small", "smile", "soap",
            "sock", "south", "spoon", "stand", "star", "sugar", "sun", "sweet", "table", "tall",
            "thunder", "time", "town", "toy", "train", "tree", "wall", "watch", "week", "west",
            "wind", "write", "year"
        ],
        "medium": [
            # 150 Medium words - Alphabetical Order
            "abandon", "absorb", "abstract", "abundant", "accurate", "acquire", "adjacent", "adjust", "admire", "advanced",
            "advocate", "allocate", "alternative", "ambitious", "analysis", "anticipate", "apparent", "appropriate", "argument", "arrangement",
            "artificial", "assistance", "assume", "attempt", "attractive", "awareness", "beneficial", "boundary", "calculate", "capacity",
            "celebrate", "circumstance", "collaborate", "combine", "communicate", "community", "compare", "compatible", "compensate", "competitive",
            "concentrate", "conclude", "conduct", "confirm", "connect", "conscious", "consider", "consistent", "construct", "consume",
            "contribute", "convenient", "convert", "cooperate", "coordinate", "corporate", "creative", "critical", "dedicate", "demonstrate",
            "depend", "detect", "determine", "diagnose", "distribute", "domestic", "efficient", "elaborate", "eliminate", "emphasize",
            "encounter", "encourage", "enormous", "essential", "evaluate", "evident", "examine", "exceed", "exclude", "expand",
            "expert", "flexible", "formulate", "frequent", "generate", "genuine", "graduate", "illustrate", "immediate", "implement",
            "implication", "incorporate", "indicate", "individual", "inevitable", "initial", "innovate", "inspire", "integrate", "intelligent",
            "interact", "internal", "interpret", "interrupt", "invest", "isolate", "justify", "maintain", "majority", "maximum",
            "mechanism", "motivate", "negotiate", "observe", "obtain", "participate", "perceive", "perspective", "potential", "precise",
            "predict", "preference", "priority", "procedure", "profession", "proficient", "prohibit", "promote", "pursue", "rational",
            "recognize", "recommend", "reflect", "regulate", "reinforce", "relevant", "require", "research", "resolve", "restrict",
            "significant", "strategy", "sufficient", "supervise", "sustainable", "technical", "temporary", "transform", "transmit", "universal", "utilize"
        ],
        "hard": [
            # 150 Hard words - Alphabetical Order (Very Difficult)
            "aberration", "abnegation", "abstemious", "acquiesce", "acrimonious", "adumbrate", "alacrity", "amalgamation", "anachronism", "antipathy",
            "apocryphal", "approbation", "arbitrary", "ascetic", "assiduous", "audacious", "belligerent", "benevolent", "bombastic", "cacophony",
            "capricious", "catharsis", "caustic", "clandestine", "cogent", "commensurate", "complacent", "conundrum", "copious", "corpulent",
            "deleterious", "demagogue", "derelict", "despot", "didactic", "dissonance", "eclectic", "effervescent", "egregious", "eloquent",
            "enigmatic", "ephemeral", "equivocal", "esoteric", "euphemism", "exacerbate", "exasperate", "exculpate", "exuberant", "facetious",
            "fastidious", "felicity", "fortuitous", "garrulous", "gratuitous", "grandiloquent", "haphazard", "harangue", "hegemony", "idiosyncrasy",
            "impassive", "impetuous", "implacable", "incognito", "indefatigable", "indolent", "ineffable", "ingenuous", "insidious", "insipid",
            "intrepid", "irascible", "juxtapose", "labyrinthine", "laconic", "loquacious", "magnanimous", "malevolent", "meticulous", "mollify",
            "munificent", "nefarious", "nonchalant", "obdurate", "obfuscate", "obsequious", "obstreperous", "panacea", "parsimonious", "pejorative",
            "pernicious", "perfidious", "perspicacious", "petulant", "plethora", "pragmatic", "precocious", "proclivity", "prodigious", "querulous",
            "quixotic", "rancorous", "recalcitrant", "recondite", "reprehensible", "sagacious", "sanctimonious", "scrupulous", "serendipity", "soliloquy",
            "spurious", "taciturn", "tenacious", "trepidation", "truculent", "ubiquitous", "unassailable", "unctuous", "vacillate", "vehement",
            "verbose", "vicarious", "vindicate", "vitriolic", "vociferous", "whimsical", "winsome", "xenophobia", "zealous", "zeppelin"
        ]
    }
    
    word_list = word_pools.get(actual_difficulty, word_pools["easy"])
    word = random.choice(word_list)
    
    return word.lower()

def get_word_sentence_usage(word):
    """Generate varied example sentences"""
    
    sentence_patterns = [
        f"Use the word in a sentence about daily life",
        f"Create a sentence showing what this word means",
        f"Make a simple example using this word",
        f"Show how children would use this word",
        f"Give a clear example with this word"
    ]
    
    pattern = random.choice(sentence_patterns)

    prompt = f"""Create ONE simple example sentence using the word "{word}".

{pattern}

RULES:
1. Sentence must be simple for children aged 6-15
2. Clearly show the word's meaning
3. Use simple vocabulary
4. Make it relatable to children
5. Be creative and varied
6. Return ONLY the sentence - no quotes
7. Use different sentence structures
8. Vary tenses

Now create a NEW, DIFFERENT sentence using "{word}"."""

    response = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.8,
        max_tokens=50
    )

    sentence = response.choices[0].message.content.strip()
    sentence = re.sub(r'^["\']+|["\']+$', '', sentence)
   
    return sentence

def get_word_meaning(word):
    """Enhanced function to explain ANY word - from simple to very complex"""
    
    prompt = f"""You are a helpful English teacher explaining the meaning of "{word}" to students aged 6-15.

CRITICAL INSTRUCTION: You MUST be able to explain ANY word - whether it's simple like "cat" or extremely complex like "perspicacious", "ubiquitous", "aberration", or "ephemeral".

FORMAT YOUR RESPONSE EXACTLY AS:
MEANING: <clear definition using simple language>
EXAMPLE: <a relatable sentence using the word>
TYPE: <noun/verb/adjective/adverb/etc>
TIP: <a helpful memory trick or tip>

RULES - ADAPT TO THE WORD:

For SIMPLE words (cat, run, happy):
- Keep explanation brief and straightforward
- Use everyday examples
- 1-2 sentences is enough

For COMPLEX words (ubiquitous, aberration, acrimonious):
- Break down the meaning into simple parts
- Use analogies or simpler synonyms first
- Explain what it means in everyday situations
- Give context that kids can understand
- Make it memorable with a trick or story

For VERY DIFFICULT words (perspicacious, obfuscate, magnanimous):
- Start with the simplest possible explanation
- Use phrases like "imagine..." or "think of it like..."
- Connect to things students already know
- Make it fun and interesting!

IMPORTANT: 
- Never say "I don't know" or "This word is too hard"
- Always provide a complete explanation
- Make examples relatable to children's lives
- Use encouraging language

Word to explain: "{word}"

Now provide the complete explanation:"""

    try:
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,  # Higher for creative explanations of complex words
            max_tokens=500    # More room for detailed explanations
        )
        
        result = response.choices[0].message.content.strip()
        
        # Verify we got a proper response
        if len(result) < 20 or "MEANING:" not in result:
            raise Exception("Invalid AI response")
            
        return result
    
    except Exception as e:
        # Robust fallback if AI fails
        print(f"Error getting meaning for '{word}': {str(e)}")
        return f"""MEANING: {word.capitalize()} is an English word. It has a specific meaning in the English language.
EXAMPLE: This is how you might use the word {word} in a sentence.
TYPE: word
TIP: If you want to learn more about '{word}', try looking it up in a dictionary or asking your teacher for help!"""

def compare_words(student_text, correct_text):
    student_words = student_text.lower().split()
    correct_words = correct_text.lower().split()
    comparison = []
    
    for i, correct_word in enumerate(correct_words):
        if i < len(student_words):
            student_word = student_words[i]
            similarity = SequenceMatcher(None, student_word, correct_word).ratio()
            
            if similarity >= 0.8:
                comparison.append({"word": correct_word, "status": "correct"})
            else:
                comparison.append({"word": correct_word, "status": "incorrect", "spoken": student_word})
        else:
            comparison.append({"word": correct_word, "status": "missing"})
    
    return comparison

def compare_spelling(student_spelling, correct_word):
    student = student_spelling.lower().strip()
    correct = correct_word.lower().strip()
    comparison = []
    max_len = max(len(student), len(correct))
    
    for i in range(max_len):
        if i < len(correct):
            correct_letter = correct[i]
            if i < len(student):
                student_letter = student[i]
                if student_letter == correct_letter:
                    comparison.append({"letter": correct_letter, "status": "correct"})
                else:
                    comparison.append({"letter": correct_letter, "status": "incorrect", "typed": student_letter})
            else:
                comparison.append({"letter": correct_letter, "status": "missing"})
    
    return comparison

# ================= ROUTES =================
@app.route("/")
def home():
    return render_template("home.html")

@app.route("/user-type")
def user_type():
    """Page to select user type (student or teacher)"""
    return render_template("user_type.html")

@app.route("/login", methods=["GET"])
def login_page():
    user_type = request.args.get("type", "student")
    return render_template("login.html", user_type=user_type)

@app.route("/login", methods=["POST"])
def login():
    data = request.json
    user_id = data.get("user_id")
    password = data.get("password")
    user_type = data.get("user_type", "student")
    
    if user_type == "teacher":
        # Teacher login - search by username
        teacher = get_teacher_by_username(user_id)  # user_id is actually username for teachers
        
        if teacher:
            # Check approval status
            status = teacher.get('status', 'approved')  # legacy accounts default to approved
            if status == 'pending':
                return jsonify({"success": False, "message": "Your account is pending admin approval. Please wait."})
            elif status == 'rejected':
                return jsonify({"success": False, "message": "Your account registration was rejected. Please contact the admin."})
            
            if check_pw(teacher['password'], password):
                # Rehash legacy plain-text passwords on first login
                if not is_hashed(teacher['password']):
                    rehash_teacher_password(teacher['_id'], password)
                session['user_id'] = teacher['_id']
                session['role'] = 'teacher'
                session['username'] = teacher.get('username', user_id)
                
                # Update last active
                update_teacher(teacher['_id'], {})
                
                return jsonify({"success": True, "redirect": "/teacher-dashboard"})
            else:
                return jsonify({"success": False, "message": "Incorrect password."})
        else:
            return jsonify({"success": False, "message": "Teacher username not found."})
    else:
        # Student login
        if user_id and password:
            user = get_user_by_id(user_id)
            
            if user:
                if check_pw(user['password'], password):
                    # Rehash legacy plain-text passwords on first login
                    if not is_hashed(user['password']):
                        rehash_user_password(user_id, password)
                    session['user_id'] = user_id
                    session['role'] = 'student'
                    session['username'] = user.get('username', user.get('name', user_id))
                    
                    # Update last active + login streak
                    update_user(user_id, {})
                    update_login_streak(user_id)
                    check_and_award_badges(user_id)  # ensure badges are up-to-date
                    
                    # Init missing fields for existing users
                    if 'achievements' not in user:
                        from database import init_user_achievements
                        update_user(user_id, {'achievements': init_user_achievements()})
                    if 'mistakes' not in user:
                        update_user(user_id, {'mistakes': {'pronunciation': [], 'spelling': [], 'vocabulary': [], 'total': 0}})
                    
                    return jsonify({"success": True, "redirect": "/main"})
                else:
                    return jsonify({"success": False, "message": "Incorrect password."})
            else:
                return jsonify({"success": False, "message": "User ID not found. Please sign up first."})
        else:
            return jsonify({"success": False, "message": "Please enter both User ID and Password."})

@app.route("/signup", methods=["GET"])
def signup_page():
    user_type = request.args.get("type", "student")
    return render_template("signup.html", user_type=user_type)

@app.route("/signup", methods=["POST"])
def signup():
    data = request.json
    user_type = data.get("user_type", "student")
    
    if user_type == "teacher":
        # Teacher signup - now requires admin approval
        username = data.get("username")
        password = data.get("password")
        name = data.get("name")
        
        if username and password and name:
            if len(username) == 6 and len(password) == 6:
                # Check if username already exists in MongoDB
                existing_teacher = get_teacher_by_username(username)
                if existing_teacher:
                    return jsonify({"success": False, "message": "Username already exists. Please choose another."})
                else:
                    # Create PENDING teacher request - requires admin approval
                    teacher_id = f"teacher_{username}"
                    teacher = create_teacher_request(teacher_id, username, password, name)
                    
                    if teacher:
                        return jsonify({
                            "success": True,
                            "pending": True,
                            "message": "Registration submitted! Your account is pending admin approval. You will be able to login once approved.",
                            "redirect": None
                        })
                    else:
                        return jsonify({"success": False, "message": "Error submitting registration. Please try again."})
            else:
                return jsonify({"success": False, "message": "Username and Password must be exactly 6 characters each."})
        else:
            return jsonify({"success": False, "message": "Please fill in all fields."})
    else:
        # Student signup
        user_id = data.get("user_id")
        password = data.get("password")
        name = data.get("name")
        student_class = data.get("class")
        division = data.get("division")
        
        if user_id and password and name and student_class and division:
            if len(user_id) == 4 and user_id.isdigit():
                # Check if user already exists in MongoDB
                existing_user = get_user_by_id(user_id)
                if existing_user:
                    return jsonify({"success": False, "message": "User ID already exists. Please login or choose a different ID."})
                else:
                    # Create user in MongoDB
                    user = create_user(user_id, name, password, user_type='student')
                    
                    if user:
                        # Update with additional info
                        update_user(user_id, {
                            'class': student_class,
                            'division': division,
                            'name': name
                        })
                        
                        session['user_id'] = user_id
                        session['role'] = 'student'
                        session['username'] = name
                        return jsonify({"success": True, "redirect": "/main"})
                    else:
                        return jsonify({"success": False, "message": "Error creating account. Please try again."})
            else:
                return jsonify({"success": False, "message": "User ID must be exactly 4 digits."})
        else:
            return jsonify({"success": False, "message": "Please fill in all fields."})

@app.route("/main")
def main():
    if 'user_id' not in session or session.get('role') != 'student':
        return redirect(url_for('home'))
    
    user_id = session['user_id']
    user_data = get_user_by_id(user_id) or {}
    
    recommended_difficulty = get_difficulty_for_level(user_data.get('level', 1))
    
    current_level = user_data.get('level', 1)
    current_xp = user_data.get('total_xp', 0)
    xp_for_current_level = get_xp_for_level(current_level)
    xp_for_next_level = get_xp_for_level(current_level + 1)
    xp_in_current_level = current_xp - xp_for_current_level
    xp_needed_for_next = xp_for_next_level - xp_for_current_level
    
    return render_template("main.html", 
                         user_id=user_id, 
                         user_data=user_data,
                         recommended_difficulty=recommended_difficulty,
                         xp_in_current_level=xp_in_current_level,
                         xp_needed_for_next=xp_needed_for_next)

@app.route("/profile")
def profile():
    if 'user_id' not in session or session.get('role') != 'student':
        return redirect(url_for('home'))
    
    user_id = session['user_id']
    user_data = get_user_by_id(user_id) or {}
    
    current_level = user_data.get('level', 1)
    current_xp = user_data.get('total_xp', 0)
    xp_for_current_level = get_xp_for_level(current_level)
    xp_for_next_level = get_xp_for_level(current_level + 1)
    xp_in_current_level = current_xp - xp_for_current_level
    xp_needed_for_next = xp_for_next_level - xp_for_current_level
    
    return render_template("profile.html",
                         user_id=user_id,
                         user_data=user_data,
                         xp_in_current_level=xp_in_current_level,
                         xp_needed_for_next=xp_needed_for_next)

@app.route("/teacher-dashboard")
def teacher_dashboard():
    if 'user_id' not in session or session.get('role') != 'teacher':
        return redirect(url_for('home'))
    
    # Get all users from MongoDB
    all_users = get_all_users()
    
    # Get all unique classes and divisions
    all_classes = set()
    all_divisions = set()
    
    for user in all_users:
        if 'class' in user:
            all_classes.add(user['class'])
        if 'division' in user:
            all_divisions.add(user['division'])
    
    all_classes = sorted(list(all_classes), key=lambda x: int(x) if x.isdigit() else 0)
    all_divisions = sorted(list(all_divisions))
    
    # Get teacher info from MongoDB
    teacher = get_teacher_by_id(session['user_id'])
    teacher_name = teacher.get('name', 'Teacher') if teacher else 'Teacher'
    
    return render_template("teacher_dashboard.html",
                         teacher_name=teacher_name,
                         all_classes=all_classes,
                         all_divisions=all_divisions)

@app.route("/get_class_students", methods=["POST"])
def get_class_students():
    """API endpoint to get students for a specific class and division"""
    if 'user_id' not in session or session.get('role') != 'teacher':
        return jsonify({"success": False, "message": "Unauthorized"})
    
    data = request.json
    selected_class = data.get("class")
    selected_division = data.get("division")
    
    # Get all users from MongoDB
    all_users = get_all_users()
    
    students = []
    for user in all_users:
        if user.get('class') == selected_class and user.get('division') == selected_division:
            students.append({
                'user_id': user.get('_id'),
                'name': user.get('name', user.get('username', 'Unknown')),
                'level': user.get('level', 1),
                'total_xp': user.get('total_xp', 0),
                'total_stars': user.get('total_stars', 0),
                'last_active': user.get('last_active', 'Never')
            })
    
    # Sort by total XP (highest first)
    students.sort(key=lambda x: x['total_xp'], reverse=True)
    
    return jsonify({
        "success": True,
        "students": students,
        "total_students": len(students)
    })

@app.route("/logout")
def logout():
    user_id = session.get('user_id')
    role = session.get('role')
    
    # Update last active in MongoDB before logout
    if role == 'student' and user_id:
        try:
            update_user(user_id, {})  # updates last_active
        except:
            pass
    
    # Clear user's conversation context on logout
    if user_id and user_id in conversation_contexts:
        del conversation_contexts[user_id]
    
    session.clear()
    return redirect(url_for('home'))

@app.route("/get_user_stats", methods=["GET"])
def get_user_stats():
    if 'user_id' not in session or session.get('role') != 'student':
        return jsonify({"success": False, "message": "Not logged in"})
    
    user_id = session['user_id']
    user_data = get_user_by_id(user_id) or {}
    
    current_level = user_data.get('level', 1)
    current_xp = user_data.get('total_xp', 0)
    xp_for_current_level = get_xp_for_level(current_level)
    xp_for_next_level = get_xp_for_level(current_level + 1)
    xp_in_current_level = current_xp - xp_for_current_level
    xp_needed_for_next = xp_for_next_level - xp_for_current_level
    
    return jsonify({
        "success": True,
        "total_xp": user_data.get('total_xp', 0),
        "total_stars": user_data.get('total_stars', 0),
        "level": current_level,
        "xp_in_current_level": xp_in_current_level,
        "xp_needed_for_next": xp_needed_for_next,
        "recommended_difficulty": get_difficulty_for_level(current_level)
    })

# ---------- CONVERSATION & ROLEPLAY ----------
@app.route("/process", methods=["POST"])
def process():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    data = request.json
    user_text = data["text"]
    roleplay = data.get("roleplay")
    user_id = session['user_id']

    if roleplay:
        ai_reply = roleplay_coach(user_text, roleplay, user_id)
        # Track roleplay activity and update practice challenge
        increment_activity(user_id, 'roleplay')
        xp_earned = update_challenge_progress(user_id, 'practice')
    else:
        ai_reply = english_coach(user_text, user_id)
        # Track conversation activity and update practice challenge
        increment_activity(user_id, 'conversation')
        xp_earned = update_challenge_progress(user_id, 'practice')

    correct = praise = question = ""
    for line in ai_reply.split("\n"):
        if line.startswith("CORRECT:"):
            correct = line.replace("CORRECT:", "").strip()
        elif line.startswith("PRAISE:"):
            praise = line.replace("PRAISE:", "").strip()
        elif line.startswith("QUESTION:"):
            question = line.replace("QUESTION:", "").strip()

    final_text = f"{correct}. {praise} {question}"
    audio = speak_to_file(final_text)

    return jsonify({
        "reply": final_text,
        "audio": audio,
        "xp_earned": xp_earned
    })

# ---------- REPEAT AFTER ME ----------
@app.route("/repeat_sentence", methods=["POST"])
def repeat_sentence():
    try:
        data = request.json
        category = data.get("category", "general")
        difficulty = data.get("difficulty", "easy")
        
        user_level = 1
        if 'user_id' in session:
            user_data = get_user_by_id(session['user_id']) or {}
            user_level = user_data.get('level', 1)
       
        sentence = generate_repeat_sentence(category, difficulty, user_level)
        audio_normal = speak_to_file(sentence, slow=False)
        audio_slow = speak_to_file(sentence, slow=True)

        return jsonify({
            "sentence": sentence,
            "audio": audio_normal,
            "audio_slow": audio_slow
        })
    except Exception as e:
        print(f"Error in repeat_sentence: {e}")
        import traceback; traceback.print_exc()
        fallback = "The cat sat on the mat."
        try:
            a1 = speak_to_file(fallback, slow=False)
            a2 = speak_to_file(fallback, slow=True)
        except:
            a1 = a2 = None
        return jsonify({"sentence": fallback, "audio": a1, "audio_slow": a2})

@app.route("/check_repeat", methods=["POST"])
def check_repeat():
    data = request.json
    student = data["student"]
    correct = data["correct"]
    stage_complete = data.get("stage_complete", False)
    accumulated_stars = data.get("accumulated_stars", 0)  # Stars from previous questions
    difficulty = data.get("difficulty", "easy")  # Difficulty for XP multiplier

    score = SequenceMatcher(None, student.lower(), correct.lower()).ratio()
    word_comparison = compare_words(student, correct)

    if score >= 0.9:
        feedback = "Perfect! Amazing pronunciation!"
        stars = 3
    elif score >= 0.75:
        feedback = "Great job! Keep practicing!"
        stars = 2
    elif score >= 0.6:
        feedback = "Good try! Try speaking more clearly."
        stars = 1
    else:
        feedback = "Keep trying! Speak slowly and clearly."
        stars = 0

    # XP multiplier based on difficulty
    xp_multiplier = {'easy': 1, 'medium': 2, 'hard': 3}.get(difficulty, 1)

    # Only save progress if stage is complete (5 sentences done)
    level_info = None
    if stage_complete and 'user_id' in session:
        total_stars_earned = accumulated_stars + stars
        # Apply difficulty multiplier
        xp_to_award = total_stars_earned * xp_multiplier
        level_info = save_user_progress(session['user_id'], xp_to_award, 'repeat')
    
    # Log mistake if score is low
    if score < 0.6 and 'user_id' in session:
        log_mistake(session['user_id'], 'pronunciation', {'sentence': correct, 'said': student, 'score': round(score * 100)})
    
    # Track mastery challenge (80%+ score)
    if score >= 0.8 and 'user_id' in session:
        update_challenge_progress(session['user_id'], 'mastery')
        increment_activity(session['user_id'], 'high_pronunciation')

    # Increment per-sentence repeat count for badge tracking
    if score >= 0.6 and 'user_id' in session:
        increment_activity(session['user_id'], 'repeat')

    # Calculate XP awarded for this stage (for frontend display)
    xp_awarded = 0
    if stage_complete and 'user_id' in session:
        total_stars_earned = accumulated_stars + stars
        xp_awarded = total_stars_earned * xp_multiplier

    return jsonify({
        "feedback": feedback,
        "score": round(score * 100),
        "stars": stars,
        "word_comparison": word_comparison,
        "level_info": level_info,
        "stars_saved": stage_complete,
        "xp_multiplier": xp_multiplier,
        "xp_awarded": xp_awarded
    })

# ---------- SPELL BEE ----------
@app.route("/spell_word", methods=["POST"])
def spell_word():
    try:
        data = request.json
        difficulty = data.get("difficulty", "easy")
        
        user_level = 1
        if 'user_id' in session:
            user_data = get_user_by_id(session['user_id']) or {}
            user_level = user_data.get('level', 1)
       
        word = generate_spell_word(difficulty, user_level)
        usage = get_word_sentence_usage(word)
       
        audio_word = speak_to_file(word, slow=True)
        audio_sentence = speak_to_file(usage, slow=False)
       
        return jsonify({
            "word": word,
            "usage": usage,
            "audio_word": audio_word,
            "audio_sentence": audio_sentence
        })
    except Exception as e:
        print(f"Error in spell_word: {e}")
        import traceback; traceback.print_exc()
        fallback_word = "cat"
        fallback_usage = "The cat is a friendly animal."
        try:
            a1 = speak_to_file(fallback_word, slow=True)
            a2 = speak_to_file(fallback_usage, slow=False)
        except:
            a1 = a2 = None
        return jsonify({"word": fallback_word, "usage": fallback_usage, "audio_word": a1, "audio_sentence": a2})

@app.route("/check_spelling", methods=["POST"])
def check_spelling():
    data = request.json
    student_spelling = data["spelling"]
    correct_word = data["correct"]
    stage_complete = data.get("stage_complete", False)
    accumulated_stars = data.get("accumulated_stars", 0)  # Stars from previous questions
    difficulty = data.get("difficulty", "easy")  # Difficulty for XP multiplier
   
    student = student_spelling.lower().strip()
    correct = correct_word.lower().strip()
   
    is_correct = (student == correct)
    letter_comparison = compare_spelling(student, correct)
   
    if is_correct:
        feedback = "🎉 Perfect! You spelled it correctly!"
        stars = 3
    else:
        similarity = SequenceMatcher(None, student, correct).ratio()
        if similarity >= 0.8:
            feedback = "Almost there! Check a few letters."
            stars = 2
        elif similarity >= 0.5:
            feedback = "Good try! Keep practicing!"
            stars = 1
        else:
            feedback = "Try again! Listen carefully to the word."
            stars = 0

    # Log spelling mistakes to the mistake tracker
    if not is_correct and 'user_id' in session:
        log_mistake(session['user_id'], 'spelling', {
            'word': correct,
            'typed': student,
            'similarity': round(SequenceMatcher(None, student, correct).ratio() * 100)
        })

    # XP multiplier based on difficulty
    xp_multiplier = {'easy': 1, 'medium': 2, 'hard': 3}.get(difficulty, 1)

    # Only save progress if stage is complete (5 words done)
    level_info = None
    if stage_complete and 'user_id' in session:
        # Save total stars (accumulated + current question)
        total_stars_earned = accumulated_stars + stars
        # Apply difficulty multiplier
        xp_to_award = total_stars_earned * xp_multiplier
        level_info = save_user_progress(session['user_id'], xp_to_award, 'spellbee')
   
    # Increment per-word spelling count for badge tracking
    if is_correct and 'user_id' in session:
        increment_activity(session['user_id'], 'spelling')

    # Calculate XP awarded for this stage (for frontend display)
    xp_awarded = 0
    if stage_complete and 'user_id' in session:
        total_stars_earned = accumulated_stars + stars
        xp_awarded = total_stars_earned * xp_multiplier

    return jsonify({
        "correct": is_correct,
        "feedback": feedback,
        "stars": stars,
        "letter_comparison": letter_comparison,
        "correct_spelling": correct,
        "level_info": level_info,
        "stars_saved": stage_complete,
        "xp_multiplier": xp_multiplier,
        "xp_awarded": xp_awarded
    })

# ---------- WORD MEANINGS ----------
@app.route("/get_meaning", methods=["POST"])
def get_meaning():
    """Enhanced route with error handling for ANY word lookup"""
    try:
        data = request.json
        word = data.get("word", "").strip()
        
        # Validate input
        if not word:
            return jsonify({
                "error": "No word provided",
                "word": "",
                "meaning": "Please enter a word to get its meaning.",
                "usage": "",
                "type": "",
                "tip": "",
                "audio": None
            }), 400
        
        # Get meaning from AI
        meaning_response = get_word_meaning(word)
       
        # Parse AI response
        meaning = usage = word_type = tip = ""
        for line in meaning_response.split("\n"):
            if line.startswith("MEANING:"):
                meaning = line.replace("MEANING:", "").strip()
            elif line.startswith("EXAMPLE:"):
                usage = line.replace("EXAMPLE:", "").strip()
            elif line.startswith("TYPE:"):
                word_type = line.replace("TYPE:", "").strip()
            elif line.startswith("TIP:"):
                tip = line.replace("TIP:", "").strip()
        
        # Fallback defaults if parsing failed
        if not meaning:
            meaning = f"The word '{word}' has a specific meaning in English."
        if not usage:
            usage = f"Here's an example: The word {word} can be used in sentences."
        if not word_type:
            word_type = "word"
        if not tip:
            tip = "Keep learning new words every day to improve your vocabulary!"
       
        # Generate audio
        audio_text = f"{word}. {meaning}. For example: {usage}. {tip}"
        audio = speak_to_file(audio_text, slow=False)
       
        # Track vocabulary activity
        if 'user_id' in session:
            increment_activity(session['user_id'], 'vocabulary')
            update_challenge_progress(session['user_id'], 'learning')
       
        return jsonify({
            "word": word,
            "meaning": meaning,
            "usage": usage,
            "type": word_type,
            "tip": tip,
            "audio": audio
        })
    
    except Exception as e:
        print(f"Error in get_meaning route: {str(e)}")
        word_safe = word if 'word' in locals() else "unknown"
        return jsonify({
            "word": word_safe,
            "meaning": f"I'm having trouble explaining '{word_safe}' right now.",
            "usage": "Please try again in a moment, or ask your teacher for help.",
            "type": "word",
            "tip": "Don't worry! You can always look up words in a dictionary too!",
            "audio": None
        }), 200

# ==================== V7 NEW ROUTES ====================

@app.route("/leaderboard")
def leaderboard():
    """Weekly class leaderboard page - accessible to both students and teachers"""
    if 'user_id' not in session:
        return redirect(url_for('home'))
    all_users = get_all_users()
    classes = sorted(list(set(str(u.get('class', '')) for u in all_users if u.get('class'))), key=lambda x: int(x) if x.isdigit() else 0)
    user_role = session.get('role', 'student')
    # For students, pre-select their own class
    user_class = ''
    user_division = ''
    if user_role == 'student':
        user_data = get_user_by_id(session['user_id']) or {}
        user_class = str(user_data.get('class', ''))
        user_division = str(user_data.get('division', ''))
    return render_template("leaderboard.html", classes=classes, user_role=user_role,
                           user_class=user_class, user_division=user_division)

@app.route("/challenges")
def challenges():
    """Weekly challenges page"""
    if 'user_id' not in session or session.get('role') != 'student':
        return redirect(url_for('home'))
    return render_template("challenges.html")

@app.route("/api/get-leaderboard", methods=["POST"])
def api_get_leaderboard():
    if 'user_id' not in session:
        return jsonify({"success": False, "message": "Not logged in"})
    data = request.json
    selected_class = data.get("class")
    selected_division = data.get("division", "")  # Optional division filter
    if not selected_class:
        return jsonify({"success": False, "message": "No class selected"})
    leaderboard_data = get_weekly_leaderboard(selected_class, selected_division)
    print(f"[LEADERBOARD] class={selected_class} division={selected_division} → {len(leaderboard_data)} students")
    return jsonify({"success": True, "leaderboard": leaderboard_data})

@app.route("/api/get-daily-challenges")
def api_get_daily_challenges():
    if 'user_id' not in session:
        return jsonify({"success": False})
    user_id = session['user_id']
    challenges = get_daily_challenges(user_id)
    if not challenges:
        return jsonify({"success": False})
    return jsonify({
        "success": True,
        "challenges": {
            "challenge1": challenges.get('challenge1', {}),
            "challenge2": challenges.get('challenge2', {}),
            "challenge3": challenges.get('challenge3', {}),
        },
        "streak": challenges.get('streak', 0),
        "completed_today": challenges.get('completed_today', 0),
        "week": challenges.get('week', '')
    })

@app.route("/api/update-challenge", methods=["POST"])
def api_update_challenge():
    if 'user_id' not in session:
        return jsonify({"success": False})
    data = request.json
    challenge_type = data.get("challenge_type")
    increment = data.get("increment", 1)
    user_id = session['user_id']
    xp_earned = update_challenge_progress(user_id, challenge_type, increment)
    challenges = get_daily_challenges(user_id)
    return jsonify({
        "success": True,
        "xp_earned": xp_earned,
        "challenges": {
            "challenge1": challenges.get('challenge1', {}),
            "challenge2": challenges.get('challenge2', {}),
            "challenge3": challenges.get('challenge3', {}),
        }
    })

@app.route("/api/ensure-badges", methods=["POST"])
def api_ensure_badges():
    """Re-run badge check for the current user to ensure all earned badges are awarded."""
    if 'user_id' not in session:
        return jsonify({"success": False})
    try:
        user_id = session['user_id']
        user = get_user_by_id(user_id)
        if user:
            # Ensure achievements dict exists
            if 'achievements' not in user:
                from database import init_user_achievements
                update_user(user_id, {'achievements': init_user_achievements()})
            # Recalculate level from total XP in case it drifted
            current_xp = user.get('total_xp', 0)
            correct_level = calculate_level(current_xp)
            if user.get('level', 1) != correct_level:
                update_user(user_id, {'level': correct_level})
            check_and_award_badges(user_id)
        return jsonify({"success": True})
    except Exception as e:
        print(f"ensure-badges error: {e}")
        return jsonify({"success": False})

@app.route("/api/get-achievements")
def api_get_achievements():
    if 'user_id' not in session:
        return jsonify({"success": False})
    user = get_user_by_id(session['user_id'])
    if not user:
        return jsonify({"success": False})
    achievements = user.get('achievements', {})
    earned_ids = achievements.get('badges_earned', [])
    badges = []
    for bid, bdata in ALL_BADGES.items():
        badges.append({
            "id": bid,
            "name": bdata['name'],
            "icon": bdata['icon'],
            "desc": bdata['desc'],
            "earned": bid in earned_ids
        })
    return jsonify({
        "success": True,
        "badges": badges,
        "stats": {
            "conversations": achievements.get('conversation_count', 0),
            "roleplays": achievements.get('roleplay_count', 0),
            "repeats": achievements.get('repeat_count', 0),
            "spellings": achievements.get('spelling_count', 0),
            "vocabulary": achievements.get('vocabulary_count', 0),
            "high_pronunciation": achievements.get('high_pronunciation_count', 0),
        }
    })

@app.route("/api/get-mistakes")
def api_get_mistakes():
    if 'user_id' not in session:
        return jsonify({"success": False})
    user = get_user_by_id(session['user_id'])
    if not user:
        return jsonify({"success": False})
    mistakes = user.get('mistakes', {'pronunciation': [], 'spelling': [], 'vocabulary': [], 'total': 0})
    return jsonify({
        "success": True,
        "pronunciation": mistakes.get('pronunciation', [])[-50:],
        "spelling": mistakes.get('spelling', [])[-50:],
        "vocabulary": mistakes.get('vocabulary', [])[-50:],
        "total": mistakes.get('total', 0)
    })

@app.route("/api/admin/migrate-users", methods=["POST"])
def admin_migrate_users():
    """Admin endpoint: recalculate all user levels + award missing badges."""
    if 'user_id' not in session or session.get('role') != 'teacher':
        return jsonify({"success": False, "message": "Unauthorized"})
    result = migrate_all_users_levels_and_badges()
    return jsonify({"success": True, "result": result})

# ==================== ADMIN ROUTES ====================

def admin_required(f):
    """Decorator to protect admin-only routes."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session or session.get('role') != 'admin':
            return redirect(url_for('admin_login_page'))
        return f(*args, **kwargs)
    return decorated

@app.route("/admin/login", methods=["GET"])
def admin_login_page():
    if session.get('role') == 'admin':
        return redirect(url_for('admin_dashboard'))
    return render_template("admin_login.html")

@app.route("/admin/login", methods=["POST"])
def admin_login():
    data = request.json
    username = data.get("username")
    password = data.get("password")
    
    admin = get_admin_by_username(username)
    if admin and check_pw(admin['password'], password):
        # Rehash legacy plain-text admin passwords on first login
        if not is_hashed(admin['password']):
            from werkzeug.security import generate_password_hash as _ghash
            update_admin(admin['_id'], {'password': _ghash(password)})
        session['user_id'] = admin['_id']
        session['role'] = 'admin'
        session['username'] = admin['username']
        update_admin(admin['_id'], {})
        return jsonify({"success": True, "redirect": "/admin/dashboard"})
    return jsonify({"success": False, "message": "Invalid admin credentials."})

@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    return render_template("admin_dashboard.html", admin_username=session.get('username'))

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login_page'))

# ---- Admin API endpoints ----

@app.route("/api/admin/stats")
@admin_required
def api_admin_stats():
    all_users = get_all_users()
    all_teachers = get_all_teachers()
    pending = [t for t in all_teachers if t.get('status', 'approved') == 'pending']
    approved = [t for t in all_teachers if t.get('status', 'approved') == 'approved']
    rejected = [t for t in all_teachers if t.get('status') == 'rejected']
    return jsonify({
        "success": True,
        "stats": {
            "total_students": len(all_users),
            "total_teachers": len(approved),
            "pending_teachers": len(pending),
            "rejected_teachers": len(rejected)
        }
    })

@app.route("/api/admin/students")
@admin_required
def api_admin_students():
    all_users = get_all_users()
    students = []
    for u in all_users:
        students.append({
            "id": u.get('_id', ''),
            "name": u.get('name', ''),
            "class": u.get('class', ''),
            "division": u.get('division', ''),
            "level": u.get('level', 1),
            "total_xp": u.get('total_xp', 0),
            "last_active": u.get('last_active', 'Never')
        })
    students.sort(key=lambda x: x['last_active'], reverse=True)
    return jsonify({"success": True, "students": students})

@app.route("/api/admin/teachers")
@admin_required
def api_admin_teachers():
    all_teachers = get_all_teachers()
    teachers = []
    for t in all_teachers:
        teachers.append({
            "id": t.get('_id', ''),
            "name": t.get('name', ''),
            "username": t.get('username', ''),
            "status": t.get('status', 'approved'),
            "created_at": t.get('created_at', ''),
            "last_active": t.get('last_active', 'Never')
        })
    teachers.sort(key=lambda x: x['created_at'], reverse=True)
    return jsonify({"success": True, "teachers": teachers})

@app.route("/api/admin/approve-teacher", methods=["POST"])
@admin_required
def api_admin_approve_teacher():
    data = request.json
    teacher_id = data.get("teacher_id")
    if not teacher_id:
        return jsonify({"success": False, "message": "No teacher ID provided."})
    result = approve_teacher(teacher_id)
    if result:
        return jsonify({"success": True, "message": "Teacher approved successfully."})
    return jsonify({"success": False, "message": "Failed to approve teacher."})

@app.route("/api/admin/reject-teacher", methods=["POST"])
@admin_required
def api_admin_reject_teacher():
    data = request.json
    teacher_id = data.get("teacher_id")
    if not teacher_id:
        return jsonify({"success": False, "message": "No teacher ID provided."})
    result = reject_teacher(teacher_id)
    if result:
        return jsonify({"success": True, "message": "Teacher rejected."})
    return jsonify({"success": False, "message": "Failed to reject teacher."})

@app.route("/api/admin/delete-teacher", methods=["POST"])
@admin_required
def api_admin_delete_teacher():
    data = request.json
    teacher_id = data.get("teacher_id")
    if not teacher_id:
        return jsonify({"success": False, "message": "No teacher ID provided."})
    result = delete_teacher(teacher_id)
    if result:
        return jsonify({"success": True, "message": "Teacher deleted."})
    return jsonify({"success": False, "message": "Failed to delete teacher."})

@app.route("/api/admin/delete-student", methods=["POST"])
@admin_required
def api_admin_delete_student():
    data = request.json
    student_id = data.get("student_id")
    if not student_id:
        return jsonify({"success": False, "message": "No student ID provided."})
    result = delete_user(student_id)
    if result:
        return jsonify({"success": True, "message": "Student deleted."})
    return jsonify({"success": False, "message": "Failed to delete student."})

@app.route("/api/admin/reset-student-password", methods=["POST"])
@admin_required
def api_admin_reset_student_password():
    data = request.json
    student_id = data.get("student_id")
    new_password = data.get("new_password")
    if not student_id or not new_password:
        return jsonify({"success": False, "message": "Missing student ID or new password."})
    if len(new_password) < 4:
        return jsonify({"success": False, "message": "Password must be at least 4 characters."})
    result = admin_reset_user_password(student_id, new_password)
    if result:
        return jsonify({"success": True, "message": "Student password reset successfully."})
    return jsonify({"success": False, "message": "Failed to reset password."})

@app.route("/api/admin/reset-teacher-password", methods=["POST"])
@admin_required
def api_admin_reset_teacher_password_route():
    data = request.json
    teacher_id = data.get("teacher_id")
    new_password = data.get("new_password")
    if not teacher_id or not new_password:
        return jsonify({"success": False, "message": "Missing teacher ID or new password."})
    if len(new_password) != 6:
        return jsonify({"success": False, "message": "Teacher password must be exactly 6 characters."})
    result = admin_reset_teacher_password(teacher_id, new_password)
    if result:
        return jsonify({"success": True, "message": "Teacher password reset successfully."})
    return jsonify({"success": False, "message": "Failed to reset password."})


# ==================== FORGOT PASSWORD (STUDENTS) ====================

@app.route("/forgot-password", methods=["GET"])
def forgot_password_page():
    return render_template("forgot_password.html", questions=SECURITY_QUESTIONS)

@app.route("/forgot-password/get-question", methods=["POST"])
def forgot_password_get_question():
    data = request.json
    user_id = data.get("user_id", "").strip()
    if not user_id or len(user_id) != 4 or not user_id.isdigit():
        return jsonify({"success": False, "message": "Enter your 4-digit User ID."})
    question = get_user_security_question(user_id)
    if question is None:
        user = get_user_by_id(user_id)
        if not user:
            return jsonify({"success": False, "message": "User ID not found."})
        return jsonify({"success": False, "message": "No security question set. Ask your teacher or admin to reset your password."})
    return jsonify({"success": True, "question": question})

@app.route("/forgot-password/verify", methods=["POST"])
def forgot_password_verify():
    data = request.json
    user_id  = data.get("user_id", "").strip()
    question = data.get("question", "").strip()
    answer   = data.get("answer", "").strip()
    new_pw   = data.get("new_password", "").strip()
    if not all([user_id, question, answer, new_pw]):
        return jsonify({"success": False, "message": "All fields are required."})
    if len(new_pw) < 4:
        return jsonify({"success": False, "message": "New password must be at least 4 characters."})
    if verify_security_answer(user_id, question, answer):
        ok = reset_student_password_by_security(user_id, new_pw)
        if ok:
            return jsonify({"success": True, "message": "Password reset! You can now log in."})
        return jsonify({"success": False, "message": "Reset failed. Please try again."})
    return jsonify({"success": False, "message": "Incorrect answer. Please try again."})

# ==================== SET SECURITY QUESTION (STUDENT) ====================

@app.route("/set-security-question", methods=["GET"])
def set_security_question_page():
    if 'user_id' not in session or session.get('role') != 'student':
        return redirect(url_for('home'))
    user = get_user_by_id(session['user_id']) or {}
    return render_template("set_security_question.html",
                           questions=SECURITY_QUESTIONS,
                           existing_question=user.get('security_question'),
                           user_data=user)

@app.route("/set-security-question", methods=["POST"])
def save_security_question():
    if 'user_id' not in session or session.get('role') != 'student':
        return jsonify({"success": False, "message": "Not logged in."})
    data = request.json
    question = data.get("question", "").strip()
    answer   = data.get("answer", "").strip()
    if not question or not answer:
        return jsonify({"success": False, "message": "Question and answer are required."})
    if question not in SECURITY_QUESTIONS:
        return jsonify({"success": False, "message": "Invalid question selected."})
    if len(answer) < 2:
        return jsonify({"success": False, "message": "Answer is too short."})
    ok = set_security_question(session['user_id'], question, answer)
    if ok:
        return jsonify({"success": True, "message": "Security question saved!"})
    return jsonify({"success": False, "message": "Failed to save. Please try again."})

# ==================== TEACHER FORGOT PASSWORD ====================

@app.route("/teacher-forgot-password", methods=["GET"])
def teacher_forgot_password_page():
    return render_template("teacher_forgot_password.html")

@app.route("/teacher-forgot-password", methods=["POST"])
def teacher_forgot_password_submit():
    data = request.json
    username = data.get("username", "").strip()
    if not username:
        return jsonify({"success": False, "message": "Enter your username."})
    teacher = get_teacher_by_username(username)
    if not teacher:
        return jsonify({"success": False, "message": "Username not found."})
    if teacher.get('status', 'approved') != 'approved':
        return jsonify({"success": False, "message": "Your account is not yet active."})
    ok = request_teacher_password_reset(teacher['_id'])
    if ok:
        return jsonify({"success": True, "message": "Request submitted! The admin has been notified. Please check back after your admin resets your password."})
    return jsonify({"success": False, "message": "Failed to submit request. Please try again."})

# ==================== ADMIN PASSWORD RESET REQUESTS ====================

@app.route("/api/admin/password-reset-requests")
@admin_required
def api_admin_password_reset_requests():
    teachers = get_teachers_requesting_password_reset()
    result = [{
        "id": t.get('_id', ''),
        "name": t.get('name', ''),
        "username": t.get('username', ''),
        "requested_at": t.get('password_reset_at', '')
    } for t in teachers]
    return jsonify({"success": True, "requests": result})

if __name__ == "__main__":
    app.run(debug=True)