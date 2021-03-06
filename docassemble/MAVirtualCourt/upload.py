from datetime import datetime
from docassemble.base.core import DAFile
from docassemble.base.functions import defined, get_user_info, interview_url
from docassemble.base.util import get_config, send_email, user_info
from psycopg2 import connect
from textwrap import dedent
from typing import Optional, Tuple

__all__ = [
  "can_access_submission",
  "get_accessible_submissions",
  "get_files",
  "initialize_db",
  "new_entry",
  "send_attachments",
  "url_for_submission"
]

db_config = get_config("filesdb")

def initialize_db():
  """
  Initialize the postgres tables for courts and files.
  We expect to have an entry 'filesdb' in the main configuration yaml.
  Below is a sample config:
  
  filesdb:
    database: uploads
    user: uploads
    password: Password1!
    host: localhost
    port: 5432
    
  This user should have access to the database (read, write)
  """
  conn = connect(**db_config)
  cur = conn.cursor()

  # creates a interviews table. has time name, document name, court name
  # in the future, we can filter by visibility on the court-basis
  cur.execute("""
  CREATE TABLE IF NOT EXISTS interviews(
    id           SERIAL        PRIMARY KEY,
    timestamp    TIMESTAMPTZ   NOT NULL,
    name         TEXT          NOT NULL,
    court_name   TEXT          NOT NULL,
    user_id      VARCHAR(50)   NOT NULL
  );
  """)
  
  # creates a files table. This stores important metadata for accessing files
  cur.execute("""
  CREATE TABLE IF NOT EXISTS files(
    id             SERIAL        PRIMARY KEY,
    number         TEXT          NOT NULL,
    filename       TEXT          NOT NULL,
    sensitive      BOOLEAN       NOT NULL DEFAULT FALSE,
    mimetype       VARCHAR(30)   NOT NULL,
    submission_id  INTEGER       REFERENCES interviews(id)
  );
  """)

  conn.commit()
  cur.close()
  conn.close()
  
def get_user_id() -> str:
  """
  Returns a unique ID for the current user
  If the user is logged in, uses their user id, otherwise uses session id.
  """
  info = get_user_info()
  
  if info:
    return str(info["id"])
  else:
    return str(user_info().session)

def get_user_email() -> Optional[str]:
  """Returns the user's email, if it exists, or None"""
  info = get_user_info()

  if info:
    return info["email"]
  else:
    return None

def new_entry(name="", court_name="", court_emails=dict(), files=[]) -> str:
  """
  Creates a new upload entry in the database
  
  Args:
    name (str): the name of who is completing the form
    doc_name (str): the name of the document
    court_name (str): the name of the court to file
    file_path (str): the path of the assembled file (on disk)

  Returns (str):
    A unique ID corresponding to the current submission
  """
  if court_name not in court_emails:
    # in our system, this should never happen, but will leave in for debugging
    raise ValueError(f"Court {court_name} does not exist")

  connection = connect(**db_config)
  submission_id = None
  
  with connection.cursor() as cursor:
    cursor.execute("INSERT INTO interviews (timestamp, name, court_name, user_id) VALUES (%s, %s, %s, %s) RETURNING id", (datetime.now(), name, court_name, get_user_id()))
    submission_id = cursor.fetchone()[0]

    for file in files:
      sensitive = defined("file.sensitive") and file.sensitive
      cursor.execute("INSERT INTO files (number, filename, mimetype, sensitive, submission_id) VALUES (%s, %s, %s, %s, %s)", (file.number, file.filename, file.mimetype, sensitive, submission_id))
    
  connection.commit()  
  connection.close()
  
  return str(submission_id)

def url_for_submission(id="") -> str:
  return interview_url(i="docassemble.MAVirtualCourt:submission.yml", id=id)

def get_court_from_email(email="", court_emails=dict()) -> Optional[str]:
  """
  """
  for [name, court_email] in court_emails.items():
    if email == court_email:
      return name

  return None

def can_access_submission(submission_id="", court_emails=dict()) -> bool:
  """
  Determines whether the current user can access the files related to the submission, submission_id

  Args:
    submission_id (str): the id of the submission we are interested in
    court_emails (dict): a dictionary mapping court names to emails

  TODO: yes, we should probably have a reverse mapping (and not iterate through it each time)

  Returns (bool):
    True if the user made the initial submission, or the user is with the court that the submission was filed; otherwise, false
  """
  connection = connect(**db_config)
  cursor = connection.cursor()
  
  cursor.execute("SELECT court_name, user_id FROM interviews WHERE id = (%s)", (submission_id,))
  entry = cursor.fetchone()

  if entry is None:
    return False
  elif get_user_id() == str(entry[1]):
    return True
  else:
    user_email = get_user_email()

    if user_email:
      return get_court_from_email(email=user_email, court_emails=court_emails)

    return False

def get_files(submission_id="", authorized=False) -> list:
  """
  Gets a list of files for the submission, submission_id and authorizes the current user access
  
  NOTE: You should ONLY call this function AFTER checking for access permissions (like through can_access_submission).

  Args:
    submission_id (str): the id of the submission to find
    authorized (bool): a flag that must be set true, reminder to use this function safely

  Returns (List[DAFile]):
    a list of DAFiles corresponding to all the files that were created in this submission
  """
  if not authorized:
    return []

  connection = connect(**db_config)
  cursor = connection.cursor()

  cursor.execute("SELECT number, filename, mimetype, sensitive FROM files WHERE submission_id = (%s)", (submission_id,))
  entry = cursor.fetchall()

  if entry is None or len(entry) == 0:
    raise ValueError(f"Could not find a submission {submission_id}")

  files = []

  # currently we do nothing with 'sensitive', but this should change when we tweak the court-email mapping
  for [number, filename, mimetype, sensitive] in entry:
    file = DAFile(number=number, filename=filename, mimetype=mimetype)
    file.user_access(get_user_id())

    files.append(file)

  return files

def get_accessible_submissions(court_emails=dict()) -> Tuple[list, str]:
  connection = connect(**db_config)
  cursor = connection.cursor()

  court_name = None
  email = get_user_email()

  if email:
    court_name = get_court_from_email(email=email, court_emails=court_emails)

  if court_name:
    cursor.execute("SELECT id, timestamp, name FROM interviews WHERE court_name = (%s)", (court_name,))
    field_name = "Name"
  else:
    user_id = get_user_id()
    cursor.execute("SELECT id, timestamp, court_name FROM interviews WHERE user_id = (%s)", (user_id,))
    field_name = "Court name"


  results = [cursor.fetchall(), field_name]

  cursor.close()
  connection.close()

  return results


def send_attachments(name="", court_name="", court_emails=dict(), files=[], submission_id="") -> bool:
  """
  Sends one or nore non-sensitive applications and submission info to the email of the court, court_name

  The email includes the user's name, submission id, and a list of non-sensitive forms. 
  If there are any sensitive forms in the files list, they will not be sent. 
  Instead, the recipient will be instructed to access the submission on our own site.

  Args:
    name (str): the name of the submitter
    court_name (str): the name of the court that should be receiving this email
    court_emails (dict): a dictionary mapping court names to emails
    files (List[DAFile]): a list of DAFiles (possibly sensitive) to be sent
    submission_id (str): the unique id for this submission

  Raises:
    ValueError if court_name is not in the court_emails dict

  Returns (bool):
    True if the email was sent successfully, false otherwise
  """
  if court_name not in court_emails:
    # in our system, this should never happen, but will leave in for debugging
    raise ValueError(f"Court {court_name} does not exist")

  attachments = [file for file in files if not defined("file.sensitive") or not file.sensitive]
  court_email = court_emails[court_name]

  filenames = [file.filename for file in attachments]
  filenames_str = "\n".join(filenames)
  submission_url = url_for_submission(id=submission_id)

  if len(attachments) != len(files):
    if len(attachments) == 0:
      body = dedent(f"""
      Dear {court_name}:

      {name} has submitted {len(files)} online. However, these file(s) have sensitive information, and will not be sent over email.
      
      Please access these forms with the following submission id: {submission_url}.
      """)

      attachments = None
    else:
      body = dedent(f"""
      Dear {court_name}:
      
      You are receiving {len(attachments)} files from {name}:
      {filenames_str}

      However, there are also {len(files) - len(attachments)} forms which are sensitive that will not be sent over email.

      Please access these forms with the following submission id: {submission_url}.
      """)
  else:
    body = dedent(f"""
    Dear {court_name}:

    You are receiving {len(attachments)} files from {name}:
    {filenames_str}

    The reference ID for these forms is {submission_url}.
    """)

  return send_email(to=court_email, subject=f"Online form submission {submission_id}", body=body, attachments=attachments)