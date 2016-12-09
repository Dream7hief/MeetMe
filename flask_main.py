import flask
from flask import render_template
from flask import request
from flask import url_for
import uuid

import json
import logging

# Date handling
import arrow  # Replacement for datetime, based on moment.js
# import datetime # But we still need time
from dateutil import tz,parser  # For interpreting local times
import datetime

from agenda import Appt, Agenda #Date and Agenda classes

# OAuth2  - Google library implementation for convenience
from oauth2client import client
import httplib2   # used in oauth2 flow

# Google API for services
from apiclient import discovery

#Random id generator
import uuid

import sys
###
# Globals
###
import CONFIG
import secrets.admin_secrets  # Per-machine secrets
import secrets.client_secrets  # Per-application secrets
#  Note to CIS 322 students:  client_secrets is what you turn in.
#     You need an admin_secrets, but the grader and I don't use yours.
#     We use our own admin_secrets file along with your client_secrets
#     file on our Raspberry Pis.

# Mongo database
from pymongo import MongoClient
MONGO_CLIENT_URL = "mongodb://{}:{}@localhost:{}/{}".format(
    secrets.client_secrets.db_user,
    secrets.client_secrets.db_user_pw,
    secrets.admin_secrets.port,
    secrets.client_secrets.db)

####
# Database connection per server process
###

try:
    dbclient = MongoClient(MONGO_CLIENT_URL)
    db = getattr(dbclient, secrets.client_secrets.db)
    collection = db.dated
except:
    print("Failure opening database.  Is Mongo running? Correct password?")
    sys.exit(1)

app = flask.Flask(__name__)
app.debug = CONFIG.DEBUG
app.logger.setLevel(logging.DEBUG)
app.secret_key = CONFIG.secret_key

SCOPES = 'https://www.googleapis.com/auth/calendar.readonly'
CLIENT_SECRET_FILE = secrets.admin_secrets.google_key_file  # You'll need this
APPLICATION_NAME = 'MeetMe class project'

#Global variable to set how many links to generate
PEOPLE_TO_INVITE = 3

#############################
#
#  Pages (routed from URLs)
#
#############################

@app.route("/")
@app.route("/index")
def index():
    app.logger.debug("Entering index")
    flask.session["user_id"] = "creator"
    flask.session["finished"] = check_completed();
    flask.session["to_finish"] = PEOPLE_TO_INVITE
    events = []
    #Check for if an event has already been made
    for record in collection.find({"user_id" : "creator"}):
        events.append(record)
    if len(events) > 0:
        return render_template('waiting.html')
    if 'begin_date' not in flask.session:
        init_session_values()
    return render_template('index.html')

@app.route("/_restart")
def restart():
    collection.drop()
    return flask.redirect(flask.url_for("index"))

@app.route("/finalize")

@app.route("/choose")
def choose():
        # We'll need authorization to list calendars
        # I wanted to put what follows into a function, but had
        # to pull it back here because the redirect has to be a
        # 'return'
    app.logger.debug("Checking credentials for Google calendar access")
    credentials = valid_credentials()
    if not credentials:
        app.logger.debug("Redirecting to authorization")
        return flask.redirect(flask.url_for('oauth2callback'))

    gcal_service = get_gcal_service(credentials)
    app.logger.debug("Returned from get_gcal_service")

    flask.g.calendars = list_calendars(gcal_service)
    if (flask.session["user_id"] == 'creator'):
        return render_template('index.html')
    else:
        return render_template('invitee.html')

@app.route("/_get_busy_times", methods=['POST'])
def get_busy_times():
    app.logger.debug("Getting busy times")

    calendars = request.form.getlist('calendar')

    begin_date = arrow.get(flask.session['begin_date']).date()
    end_date = arrow.get(flask.session['end_date']).date()
    start_time = arrow.get(interpret_time(flask.session['startTime'])).replace(seconds =+ 1)
    end_time = arrow.get(interpret_time(flask.session['endTime']))

    begin_date_and_time = combine_date_and_time(begin_date,start_time)
    end_date_and_time = combine_date_and_time(end_date, end_time)

    gcal_service = get_gcal_service(valid_credentials())
    result = Agenda()
    flask.g.free_times = []
    for calID in calendars: #Gets all calendars
        events = gcal_service.events().list(calendarId=calID,timeMin=begin_date_and_time, timeMax=end_date_and_time,singleEvents=True,orderBy='startTime').execute()
        modified_events = restrict_events_not_in_range(events, start_time, end_time)

        for i in range(len(modified_events)): #Gets all events
            # title = modified_events[i]['summary']
            start = modified_events[i]['start']['dateTime']
            end = modified_events[i]['end']['dateTime']
            datetime_start = parser.parse(start)
            datetime_end = parser.parse(end)
            appointment = Appt(datetime_start.date(),datetime_start.time(), datetime_end.time())
            result.append(appointment)

    day_span = [day.datetime for day in arrow.Arrow.range('day', parser.parse(begin_date_and_time), (parser.parse(end_date_and_time)))]
    all_times = []
    for day in day_span:
        time_range = Appt(day.date(), parser.parse(begin_date_and_time).time(), parser.parse(end_date_and_time).time())

        complement = result.complement(time_range)
        complement = str(complement)

        if ("\n" in str(complement)):
            tmp = complement.split("\n")
            for time in tmp:
                flask.g.free_times.append(time)
        else:
            flask.g.free_times.append(complement)

    flask.g.free_times = sorted(flask.g.free_times)

    flask.session['free_times'] = flask.g.free_times

    clear_db() # removes all other unique id's from this database, but not the creators
    if collection.find({"user_id" : flask.session["user_id"]}):
        remove_from_mongo(flask.session["user_id"])
    store_in_mongo(flask.g.free_times, flask.session["user_id"], True)

    if flask.session["user_id"] == 'creator':
        return render_template('index.html')
    else:
        return render_template('invitee.html')

@app.route("/invite_people", methods=['POST'])
def invite_people():
    empty_free_times = []
    set_of_ids = set()
    while len(set_of_ids) < PEOPLE_TO_INVITE :
        set_of_ids.add(uuid.uuid4())
    for ids in set_of_ids:
        store_in_mongo(empty_free_times, ids, False)
        flask.flash("localhost:5000/invitee/{}".format(ids))

    return render_template('invite_page.html')

@app.route('/invitee/<user_id>')
def get_invitee_free_times(user_id):
    if (collection.find({"user_id" : user_id})):
        flask.session["user_id"] = user_id
        for record in collection.find({"user_id" : "creator"}):
            flask.session["daterange"] = record['daterange']
            flask.session["start_time"] = record['start_time']
            flask.session["end_time"] = record['end_time']
    return render_template('invitee.html')

@app.route('/invitee_end', methods = ['POST'])
def invitee_end():

    return render_template('invitee_end.html')
####
#
#  Google calendar authorization:
#      Returns us to the main /choose screen after inserting
#      the calendar_service object in the session state.  May
#      redirect to OAuth server first, and may take multiple
#      trips through the oauth2 callback function.
#
#  Protocol for use ON EACH REQUEST:
#     First, check for valid credentials
#     If we don't have valid credentials
#         Get credentials (jump to the oauth2 protocol)
#         (redirects back to /choose, this time with credentials)
#     If we do have valid credentials
#         Get the service object
#
#  The final result of successful authorization is a 'service'
#  object.  We use a 'service' object to actually retrieve data
#  from the Google services. Service objects are NOT serializable ---
#  we can't stash one in a cookie.  Instead, on each request we
#  get a fresh serivce object from our credentials, which are
#  serializable.
#
#  Note that after authorization we always redirect to /choose;
#  If this is unsatisfactory, we'll need a session variable to use
#  as a 'continuation' or 'return address' to use instead.
#
####

def valid_credentials():
    """
    Returns OAuth2 credentials if we have valid
    credentials in the session.  This is a 'truthy' value.
    Return None if we don't have credentials, or if they
    have expired or are otherwise invalid.  This is a 'falsy' value.
    """
    if 'credentials' not in flask.session:
        return None

    credentials = client.OAuth2Credentials.from_json(
        flask.session['credentials'])

    if (credentials.invalid or
            credentials.access_token_expired):
        return None
    return credentials

def get_gcal_service(credentials):
    """
    We need a Google calendar 'service' object to obtain
    list of calendars, busy times, etc.  This requires
    authorization. If authorization is already in effect,
    we'll just return with the authorization. Otherwise,
    control flow will be interrupted by authorization, and we'll
    end up redirected back to /choose *without a service object*.
    Then the second call will succeed without additional authorization.
    """
    app.logger.debug("Entering get_gcal_service")
    http_auth = credentials.authorize(httplib2.Http())
    service = discovery.build('calendar', 'v3', http=http_auth)
    app.logger.debug("Returning service")
    return service

@app.route('/oauth2callback')
def oauth2callback():
    """
    The 'flow' has this one place to call back to.  We'll enter here
    more than once as steps in the flow are completed, and need to keep
    track of how far we've gotten. The first time we'll do the first
    step, the second time we'll skip the first step and do the second,
    and so on.
    """
    app.logger.debug("Entering oauth2callback")
    flow = client.flow_from_clientsecrets(
        CLIENT_SECRET_FILE,
        scope=SCOPES,
        redirect_uri=flask.url_for('oauth2callback', _external=True))
    # Note we are *not* redirecting above.  We are noting *where*
    # we will redirect to, which is this function.

    # The *second* time we enter here, it's a callback
    # with 'code' set in the URL parameter.  If we don't
    # see that, it must be the first time through, so we
    # need to do step 1.
    app.logger.debug("Got flow")
    if 'code' not in flask.request.args:
        app.logger.debug("Code not in flask.request.args")
        auth_uri = flow.step1_get_authorize_url()
        return flask.redirect(auth_uri)
        # This will redirect back here, but the second time through
        # we'll have the 'code' parameter set
    else:
        # It's the second time through ... we can tell because
        # we got the 'code' argument in the URL.
        app.logger.debug("Code was in flask.request.args")
        auth_code = flask.request.args.get('code')
        credentials = flow.step2_exchange(auth_code)
        flask.session['credentials'] = credentials.to_json()
        # Now I can build the service and execute the query,
        # but for the moment I'll just log it and go back to
        # the main screen
        app.logger.debug("Got credentials")
        return flask.redirect(flask.url_for('choose'))

#####
#
#  Option setting:  Buttons or forms that add some
#     information into session state.  Don't do the
#     computation here; use of the information might
#     depend on what other information we have.
#   Setting an option sends us back to the main display
#      page, where we may put the new information to use.
#
#####

@app.route('/setrange', methods=['POST'])
def setrange():
    """
    User chose a date range with the bootstrap daterange
    widget.
    """
    app.logger.debug("Entering setrange")
    # flask.flash("Setrange gave us '{}'".format(
    #     request.form.get('daterange')))
    daterange = request.form.get('daterange')
    flask.session['daterange'] = daterange
    flask.session['startTime'] = request.form.get('startTime')
    flask.session['endTime'] = request.form.get('endTime')
    daterange_parts = daterange.split()
    flask.session['begin_date'] = interpret_date(daterange_parts[0])
    flask.session['end_date'] = interpret_date(daterange_parts[2])
    app.logger.debug("Setrange parsed {} - {}  dates as {} - {}".format(
        daterange_parts[0], daterange_parts[1],
        flask.session['begin_date'], flask.session['end_date']))
    return flask.redirect(flask.url_for("choose"))

####
#
#   Initialize session variables
#
####

def init_session_values():
    """
    Start with some reasonable defaults for date and time ranges.
    Note this must be run in app context ... can't call from main.
    """
    # Default date span = tomorrow to 1 week from now
    now = arrow.now('local')     # We really should be using tz from browser
    tomorrow = now.replace(days=+1)
    nextweek = now.replace(days=+7)
    flask.session["begin_date"] = tomorrow.floor('day').isoformat()
    flask.session["end_date"] = nextweek.ceil('day').isoformat()
    flask.session["daterange"] = "{} - {}".format(
        tomorrow.format("MM/DD/YYYY"),
        nextweek.format("MM/DD/YYYY"))


def interpret_time(text):
    """
    Read time in a human-compatible format and
    interpret as ISO format with local timezone.
    May throw exception if time can't be interpreted. In that
    case it will also flash a message explaining accepted formats.
    """
    app.logger.debug("Decoding time '{}'".format(text))
    time_formats = ["h:mma",  "h:mm a", "H:mm", "ha"]
    try:
        as_arrow = arrow.get(text, time_formats).replace(tzinfo=tz.tzlocal())
        as_arrow = as_arrow.replace(year=2016)  # HACK see below
        app.logger.debug("Succeeded interpreting time")
    except:
        app.logger.debug("Failed to interpret time")
        flask.flash("Time '{}' didn't match accepted formats 13:30 or 1:30pm"
                    .format(text))
        raise
    return as_arrow.isoformat()
    # HACK #Workaround
    # isoformat() on raspberry Pi does not work for some dates
    # far from now.  It will fail with an overflow from time stamp out
    # of range while checking for daylight savings time.  Workaround is
    # to force the date-time combination into the year 2016, which seems to
    # get the timestamp into a reasonable range. This workaround should be
    # removed when Arrow or Dateutil.tz is fixed.
    # FIXME: Remove the workaround when arrow is fixed (but only after testing
    # on raspberry Pi --- failure is likely due to 32-bit integers on that
    # platform)

def interpret_date(text):
    """
    Convert text of date to ISO format used internally,
    with the local time zone.
    """
    try:
        as_arrow = arrow.get(text, "MM/DD/YYYY").replace(
            tzinfo=tz.tzlocal())
    except:
        flask.flash("Date '{}' didn't fit expected format 12/31/2001")
        raise
    return as_arrow.isoformat()

def next_day(isotext):
    """
    ISO date + 1 day (used in query to Google calendar)
    """
    as_arrow = arrow.get(isotext)
    return as_arrow.replace(days=+1).isoformat()

####
#
#  Functions (NOT pages) that return some information
#
####

def list_calendars(service):
    """
    Given a google 'service' object, return a list of
    calendars.  Each calendar is represented by a dict.
    The returned list is sorted to have
    the primary calendar first, and selected (that is, displayed in
    Google Calendars web app) calendars before unselected calendars.
    """
    app.logger.debug("Entering list_calendars")
    calendar_list = service.calendarList().list().execute()["items"]
    result = []
    for cal in calendar_list:
        kind = cal["kind"]
        id = cal["id"]
        if "description" in cal:
            desc = cal["description"]
        else:
            desc = "(no description)"
        summary = cal["summary"]
        # Optional binary attributes with False as default
        selected = ("selected" in cal) and cal["selected"]
        primary = ("primary" in cal) and cal["primary"]
        result.append(
            {"kind": kind,
             "id": id,
             "summary": summary,
             "selected": selected,
             "primary": primary
             })
    return sorted(result, key=cal_sort_key)

def cal_sort_key(cal):
    """
    Sort key for the list of calendars:  primary calendar first,
    then other selected calendars, then unselected calendars.
    (" " sorts before "X", and tuples are compared piecewise)
    """
    if cal["selected"]:
        selected_key = " "
    else:
        selected_key = "X"
    if cal["primary"]:
        primary_key = " "
    else:
        primary_key = "X"
    return (primary_key, selected_key, cal["summary"])

def convert_iso_to_human(iso_time):
    # Typical Isoformatted time: 2016-11-10T21:00:00-08:00
    # Typical Human Time: 9PM
    ending = 'AM'
    date = arrow.get(iso_time[:10]).format("ddd, D")
    time = iso_time[11:16]
    if int(time[:2]) == 0:
        time = '12' + time[2:]
    elif int(time[:2]) >= 12:
        time = str(int(time[:2]) - 12) + time[2:]
        ending = 'PM'
    else:
        time = str(int(time[:2])) + time[2:]
    return (date + ' - ' + time + ending)

def combine_date_and_time(date,time):
    date = arrow.get(date).format("YYYY-MM-DD") #gets just the date of the arrow representation
    time = time.format("HH:mm") #gets just the time of the arrow representation
    date_and_time = arrow.get(date + ' ' + time).replace(tzinfo=tz.tzlocal()).isoformat() #combines the two times and dates
    return date_and_time

def restrict_events_not_in_range(events, start_time, end_time):
    modified_events = []
    for i in range(len(events['items'])):
        try:
            title = events['items'][i]['summary']

            start = arrow.get(events['items'][i]['start']['dateTime']).format('hh:mma')
            start = interpret_time(start)

            end = arrow.get(events['items'][i]['end']['dateTime']).format('hh:mma')
            end = interpret_time(end)

            interpretted_start_time = interpret_time(start_time.format('hh:mma'))
            interpretted_end_time = interpret_time(end_time.format('hh:mma'))

            if ((end >= interpretted_start_time and end <= interpretted_end_time) or \
            (start <= interpretted_end_time and start >= interpretted_start_time)):
                modified_events.append(events['items'][i])
        except KeyError:
            pass
    return modified_events

def clear_db():
    collection.remove({"user_id": {"$ne": "creator"}})

def store_in_mongo(free_times_list, user_id, is_done):
    record = {"available_times" : free_times_list,
              "user_id" : user_id,
              "start_time" : flask.session['startTime'],
              "end_time" : flask.session['endTime'],
              "daterange" : flask.session['daterange'],
              "is_done" : is_done}
    collection.insert(record)

def remove_from_mongo(user_id):
    collection.remove({"user_id": user_id})

def check_completed():
    ctr = -1
    for record in collection.find():
        if (record['is_done']):
            ctr += 1
    return ctr

#################
#
# Functions used within the templates
#
#################

@app.template_filter('fmtdate')
def format_arrow_date(date):
    try:
        normal = arrow.get(date)
        return normal.format("ddd MM/DD/YYYY")
    except:
        return "(bad date)"

@app.template_filter('fmttime')
def format_arrow_time(time):
    try:
        normal = arrow.get(time)
        return normal.format("HH:mm")
    except:
        return "(bad time)"

#############

if __name__ == "__main__":
    # App is created above so that it will
    # exist whether this is 'main' or not
    # (e.g., if we are running under green unicorn)
    app.run(port=CONFIG.PORT, host="0.0.0.0")
