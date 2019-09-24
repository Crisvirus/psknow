#!/usr/bin/env python3

import jwt
import datetime
import tempfile
import os
import traceback

from .config import Configuration
from .wrappers import not_admin
from .scheduler import Scheduler
from .database_helper import update_hs_id, generic_find
from .process import Process
from .backend import get_cracked_tuple, get_uncracked_tuple

from copy import deepcopy
from flask import render_template, request, redirect, flash, url_for, Blueprint, send_from_directory, jsonify
from functools import wraps
from flask_login import login_required, current_user

key_template = {
    "user": "",
    "date_generated": "",
    "key_id": 0,
    "name": ""
}

api_api = Blueprint('api_api', __name__)


# Decorator that determines if a user is allowd to use the API
def allowed_api(f):
    @wraps(f)
    def allowed_api_fct(*args, **kwargs):
        crt_user = current_user.get_id()
        user_entry = Configuration.users.find_one({"username": crt_user})  # TODO make a try except. Check for none

        # Check if user is authorised to use an API
        try:
            if user_entry["allow_api"] is not True:
                flash("Forbidden!")
                Configuration.logger.warning("User '%s' tried accessing %s without being API allowed" %
                                             (crt_user, request.base_url))
                return redirect(url_for('api_api.main_api'))
        except KeyError:
            Configuration.logger.error("User entry does not contain 'allow_api' key: %s" % user_entry)
            flash("Server error!")
            return redirect(url_for('api_api.main_api'))

        kwargs["user_entry"] = user_entry
        return f(*args, **kwargs)
    return allowed_api_fct


# Decorator that checks the validity of a API key sent
def require_key(f):
    @wraps(f)
    def require_key_fct(*args, **kwargs):
        api_key = request.form.get("apikey", None)

        if api_key is None:
            return jsonify({"success": False, "reason": "Api key missing!"})

        try:
            decoded_api_key = jwt_decode(api_key, Configuration.api_secret_key)
        except (jwt.exceptions.InvalidSignatureError, jwt.exceptions.DecodeError):
            return jsonify({"success": False, "reason": "Invalid API key!"})

        user_entry = Configuration.users.find_one({"username": decoded_api_key["user"]})  # TODO make a try except. Check for none

        try:
            if api_key not in user_entry["api_keys"] or user_entry["allow_api"] is not True:
                return jsonify({"success": False, "reason": "Forbidden, invalid or expired API key!"})
        except KeyError:
            Configuration.logger.warning("User entry does not contain 'api_keys' or 'allow_api' key: %s" % user_entry)

        kwargs["user_entry"] = user_entry
        kwargs["apikey"] = api_key

        return f(*args, **kwargs)
    return require_key_fct


# Decorator that checks if the API currently has any job running
def has_job(f):
    @wraps(f)
    def has_job_fct(*args, **kwargs):
        cursor, error = Scheduler.get_reserved(kwargs["apikey"])
        data = next(cursor, None)
        if error != "":
            return jsonify({"success": False, "reason": error})

        if data is None:
            return jsonify({"success": False, "reason": "No running job on this API key."})

        kwargs["job"] = data

        return f(*args, **kwargs)
    return has_job_fct


def job_running(f):
    @wraps(f)
    def job_running_fct(*args, **kwargs):
        try:
            run_status = kwargs["job"]["reserved"]["status"]
        except KeyError:
            return jsonify({"success": False, "reason": "No running job on this API key or missing key"})

        if run_status != "running":
            return jsonify({"success": False, "reason": "Job for this API is paused"})

        return f(*args, **kwargs)
    return job_running_fct


def exception_catcher(f):
    @wraps(f)
    def exception_catcher_fct(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            Configuration.logger.error("Caught unexpected exception: '%s', '%s'" % (traceback.format_exc(), e))
            return jsonify({"success": False, "reason": "Unexpected error"})
    return exception_catcher_fct


# Helper funtion that returns a dictionary from a utf-8 encoded jwt
def jwt_decode(token, api_key):
    return jwt.decode(token.encode("utf-8"), api_key)


# Helper funtion that create a jwt token from a dictionary then encodes it in utf8
def jwt_encode(dic, api_key):
    return jwt.encode(dic, api_key, algorithm='HS512').decode("utf-8")


@api_api.route('/api/', methods=['GET'])
@login_required
@not_admin
def main_api():
    user_entry = Configuration.users.find_one({"username": current_user.get_id()})  # TODO make a try except. Check for none

    api_keys = []
    try:
        for key in user_entry["api_keys"]:
            entry = dict()

            entry["key"] = key

            # The jwt comes from the database, no need to check for validity
            values = jwt_decode(entry["key"], Configuration.api_secret_key)
            entry["name"] = values["name"]
            entry["date_generated"] = datetime.datetime.strptime(values["date_generated"], '%Y-%m-%dT%H:%M:%S.%f')\
                .strftime('%H:%M - %d.%m.%Y')

            api_keys.append(entry)
    except KeyError:
        Configuration.logger.warning("User entry does not contain 'api_keys' key: %s" % user_entry)

    return render_template('api.html', logged_in=True, api_keys=api_keys)


@api_api.route('/api/autoupload.py', methods=['GET'])
@login_required
@not_admin
def send_autoupload():
    return send_from_directory(api_api.static_folder, "autoupload.py")


@api_api.route('/api/generate-key/', methods=['POST'])
@login_required
@not_admin
@allowed_api
def generate_key(**kwargs):
    api_key = deepcopy(key_template)

    try:
        user_entry = kwargs["user_entry"]
        crt_user = user_entry["username"]
    except KeyError:
        flash("Server error!")
        Configuration.logger.error("Expected attribute 'user_entry' missing from decorator. Got: %s" % kwargs)
        return redirect(url_for('api_api.main_api'))

    try:
        new_id = str(1000 + len(user_entry["api_keys"]))
    except KeyError:
        Configuration.logger.error("User entry does not contain 'api_keys' key: %s" % user_entry)
        flash("Server error!")
        return redirect(url_for('api_api.main_api'))

    # Generate key from user + date_generated + key id + name
    api_key["user"] = crt_user
    api_key["date_generated"] = datetime.datetime.now().isoformat()
    api_key["key_id"] = new_id
    api_key["name"] = request.form.get("keyname", "unnamed")

    user_entry["api_keys"].append(jwt_encode(api_key, Configuration.api_secret_key))

    # TODO make a try except
    Configuration.users.update_one({"username": crt_user}, {"$set": user_entry})
    flash("API key generated successfully!", category='success')

    return redirect(url_for('api_api.main_api'))


@api_api.route('/api/v1/getwifis', methods=['POST'])
@exception_catcher
@require_key
def getwifis_v1(**kwargs):
    wifis, err = generic_find(Configuration.wifis, {"users": kwargs["user_entry"]["username"]}, api_query=True)
    if err:
        return jsonify({"success": False, "reason": "Database error."})

    result = {"cracked": [], "uncracked": []}
    for wifi in wifis:
        if wifi["handshake"]["password"] == "":
            result["uncracked"].append(get_uncracked_tuple(wifi))
        else:
            result["cracked"].append(get_cracked_tuple(wifi))

    return jsonify({"success": True, "data": result})


@api_api.route('/api/v1/getwork', methods=['POST'])
@exception_catcher
@require_key
def getwork_v1(**kwargs):
    # Check if user has any running jobs
    has_reserved, error = Scheduler.has_reserved(kwargs["apikey"])
    if error != "":
        return jsonify({"success": False, "reason": error})

    if has_reserved:
        return jsonify({"success": False, "reason": "This API key already has work reserved. "
                                                    "Resume or cancel current job."})

    capabilities = request.form.getlist("capabilities", None)

    if capabilities is None:
        return jsonify({"success": False, "reason": "Capabilities were not sent!"})

    work, error = Scheduler.get_next_handshake(kwargs["apikey"], capabilities)
    if error != "":
        return jsonify({"success": False, "reason": error})

    return jsonify({"success": True, "data": work})


def file_ok(filename, apikey):
    if '/' in filename:
        Configuration.logger.warning("API key '%s' is trying to traverse!" % apikey)
        return False

    if filename is None or filename == "" or filename not in Configuration.api_file_names:
        return False

    return True


@api_api.route('/api/v1/getfile', methods=['POST'])
@exception_catcher
@require_key
def getfile_v1(**kwargs):
    filename = request.form.get("file", None)

    if file_ok(filename, kwargs["apikey"]):
        return ""

    return send_from_directory(Configuration.application.static_folder, filename)


@api_api.route('/api/v1/checkfile', methods=['POST'])
@exception_catcher
@require_key
def checkfile_v1(**kwargs):
    filename = request.form.get("file", None)

    if file_ok(filename, kwargs["apikey"]):
        return jsonify({"success": True})

    return jsonify({"success": False, "reason": "File key missing!"})


@api_api.route('/api/v1/getmissing', methods=['POST'])
@exception_catcher
@require_key
def getmissing_v1(**_):
    capabilities = request.form.getlist("capabilities", None)

    if capabilities is None:
        return jsonify({"success": False, "reason": "Capabilities were not sent!"})

    rule_reqs = set()
    response = []
    for rule in Configuration.get_active_rules():
        for req in rule["reqs"]:
            if req not in capabilities and req not in rule_reqs:
                rule_reqs.add(req)
                entry = {"type": "file"}

                if req == "hashcat" or req == "john":
                    entry["type"] = "program"
                    entry["name"] = req
                elif rule["type"] == "john":
                    entry["path"] = rule["baselist"]
                elif rule["type"] == "wordlist":
                    entry["path"] = rule["wordlist"]
                elif rule["type"] == "generated":
                    entry["path"] = rule["command"].split()[0]
                elif rule["type"] == "filemask_hashcat":
                    entry["path"] = rule["filemask_path"]
                else:
                    Configuration.logger.error("Unknown type of rule '%s'" % rule["type"])
                    return jsonify({"success": False, "reason": "Rule requirement error!"})
                response.append(entry)

    return jsonify({"success": True, "data": response})


# TODO update pause to work like this - if the user pauses give a week to resume
# TODO if a user does not pause give double of crack time then erase
@api_api.route('/api/v1/pausework', methods=['POST'])
@exception_catcher
@require_key
@has_job
def pausework_v1(**kwargs):
    if kwargs["job"]["reserved"]["status"] != "running":
        return jsonify({"success": False, "reason": "Job is already paused"})

    if update_hs_id(kwargs["job"]["id"], {"reserved.status": "paused", "handshake.eta": "Paused"}):
        return jsonify({"success": False, "reason": "Database error while updating pause status."})

    return jsonify({"success": True})


@api_api.route('/api/v1/resumework', methods=['POST'])
@exception_catcher
@require_key
@has_job
def resumework_v1(**kwargs):
    if kwargs["job"]["reserved"]["status"] != "paused":
        return jsonify({"success": False, "reason": "Job is not paused"})

    if update_hs_id(kwargs["job"]["id"], {"reserved.status": "running", "handshake.eta": "Not available"}):
        return jsonify({"success": False, "reason": "Database error while updating continue status."})

    return jsonify({"success": True})


@api_api.route('/api/v1/stopwork', methods=['POST'])
@exception_catcher
@require_key
@has_job
def stopwork_v1(**kwargs):
    if Scheduler.release_handshake(kwargs["job"]["id"]):
        return jsonify({"success": False, "reason": "Database error while stopping work."})

    return jsonify({"success": True})


@api_api.route('/api/v1/sendeta', methods=['POST'])
@exception_catcher
@require_key
@has_job
@job_running
def sendeta_v1(**kwargs):
    eta = request.form.get("eta", None)

    if eta is None:
        return jsonify({"success": False, "reason": "ETA field missing!"})

    if Configuration.allowed_eta_regex.match(eta) is None:
        return jsonify({"success": False, "reason": "Forbidden characters in eta."})

    if update_hs_id(kwargs["job"]["id"], {"handshake.eta": eta}):
        return jsonify({"success": False, "reason": "Database error while updating eta."})

    return jsonify({"success": True})


def is_password(password, db_entry):
    _, temp_filename = tempfile.mkstemp(prefix="psknow_backend")

    with open(temp_filename, "w") as fd:
        fd.write(password)

    try:
        wifi_hash = db_entry["path"]
        if db_entry["file_type"] == "16800":
            pmkid = ""

            with open(db_entry["path"]) as fd:
                for line in fd:
                    matchobj = Configuration.pmkid_regex.match(line)
                    if matchobj.group(1) == db_entry["handshake"]["MAC"].replace(":", ""):
                        pmkid = line.replace("\n", "")
                        break

            if pmkid == "":
                Configuration.logger.error("Could not match database MAC to 16800 file to retrieve PMKID")
                return False, "Failed to retrieve PMKID from file for password check."

            wifi_hash = "-I %s" % pmkid

        command = 'aircrack-ng %s -w %s --bssid %s' % (wifi_hash, temp_filename, db_entry["handshake"]["MAC"])

        process = Process(command)
        output = process.stdout()

        if process.poll() != 0:
            Configuration.logger.error("Aircrack crashed with error '%s'" % process.stderr())
            return False, "Aircrack crashed unexpectedly."

        if "KEY FOUND!" in output:
            return True, ""

        Configuration.logger.warning("Provided password '%s' does not match!" % password)
        return False, "Password does not match!"

    except Exception as e:
        Configuration.logger.error("Exception raised while checking password: '%s'" % e)
        return False, "Unexpected exception in password checking."
    finally:
        os.remove(temp_filename)


# Decorator that checks the validity of a API key sent
def not_cracked(f):
    @wraps(f)
    def fct(*args, **kwargs):
        password = request.form.get("password", None)

        if password is None:
            return jsonify({"success": False, "reason": "Password field missing."})

        if password != "":
            kwargs["password"] = password
            return f(*args, **kwargs)

        updated = dict()
        updated["handshake.tried_dicts"].append(kwargs["job"]["reserved"]["tried_rule"])
        updated["handshake.active"] = False
        updated["reserved"] = None

        if update_hs_id(kwargs["job"]["id"], updated):
            return jsonify({"success": False, "reason": "Error updating database."})

        return jsonify({"success": True})
    return fct


@api_api.route('/api/v1/sendresult', methods=['POST'])
@exception_catcher
@require_key
@has_job
@not_cracked
def sendresult_v1(**kwargs):
    password = kwargs["password"]

    if len(password) < 8 or len(password) > 63:
        return jsonify({"success": False, "reason": "Invalid password length."})

    wifi_entry = kwargs["job"]
    is_pass, error = is_password(password, wifi_entry)
    if not is_pass:
        return jsonify({"success": False, "reason": error})

    handshake = wifi_entry["handshake"]
    handshake["password"] = password
    handshake["date_cracked"] = datetime.datetime.now()
    handshake["cracked_rule"] = wifi_entry["reserved"]["tried_rule"]
    handshake["tried_dicts"].append(wifi_entry["reserved"]["tried_rule"])
    handshake["active"] = False

    Configuration.logger.info("Cracked handshake '%s' - '%s': '%s'" % (handshake["SSID"], handshake["MAC"], password))

    if update_hs_id(wifi_entry["id"], {"handshake": handshake, "reserved": None}):
        return jsonify({"success": False, "reason": "Error updating database."})

    return jsonify({"success": True})