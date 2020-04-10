# import packages
import boto3
import json
import decimal
import configparser
import os
from urllib.request import Request, urlopen, URLError, HTTPError
from urllib.parse import urlencode
from datetime import datetime
from dateutil import parser
from base64 import b64decode
from boto3.dynamodb.conditions import Key, Attr
from botocore.exceptions import ClientError
from botocore.config import Config

# date differential function
def diff_dates(strDate1, strDate2):
    intSecs = float(strDate2)-float(strDate1)
    return intSecs

# dynamoDB function
def update_ddb(objTable, strArn, strUpdate, now, intHours):
    response = objTable.put_item(
      Item ={
        'arn' : strArn,
        'lastUpdatedTime' : strUpdate,
        'added' : now,
        'ttl' : int(now) + int(intHours) + 3600
      }
    )

# aws.health affected accounts
def get_healthAccounts (awshealth, event, strArn, awsRegion):
    affectedAccounts = []
    event_accounts_paginator = awshealth.get_paginator('describe_affected_accounts_for_organization')
    event_accounts_page_iterator = event_accounts_paginator.paginate(
        eventArn=strArn
        )
    for event_accounts_page in event_accounts_page_iterator:
        json_event_accounts = json.dumps(event_accounts_page, cls=DatetimeEncoder)
        parsed_event_accounts = json.loads (json_event_accounts)
        affectedAccounts = affectedAccounts + (parsed_event_accounts['affectedAccounts'])
    return affectedAccounts
        
# aws.health affected entities (aka resources)
def get_healthEntities (awshealth, event, strArn, awsRegion, affectedAccounts):
    if len(affectedAccounts) >= 1:
        affectedAccounts = affectedAccounts[0]
        event_entities_paginator = awshealth.get_paginator('describe_affected_entities_for_organization')
        event_entities_page_iterator = event_entities_paginator.paginate(
          organizationEntityFilters=[
            {    
                'awsAccountId': affectedAccounts,
                'eventArn': strArn
            }
          ]
        )
        affectedEntities = []
        for event_entities_page in event_entities_page_iterator:
            json_event_entities = json.dumps(event_entities_page, cls=DatetimeEncoder)
            parsed_event_entities = json.loads (json_event_entities)
            for entity in parsed_event_entities['entities']:
                affectedEntities.append(entity['entityValue'])
        return affectedEntities
    else:
        affectedEntities = ['All resources\nin region']
        return affectedEntities
    
# aws.health message for chime  
def get_healthUpdates(awshealth, event, strArn, awsRegion, affectedAccounts):
    if len(affectedAccounts) >= 1:
        affectedAccounts = affectedAccounts[0]
        event_details = awshealth.describe_event_details_for_organization (
          organizationEventDetailFilters=[
            {
                'awsAccountId': affectedAccounts,
                'eventArn': strArn
            }
          ]
        )
        json_event_details = json.dumps(event_details, cls=DatetimeEncoder)
        parsed_event_details = json.loads (json_event_details)
        healthUpdates = (parsed_event_details['successfulSet'][0]['eventDescription']['latestDescription'])
        return healthUpdates
    # needed for Service Health Dashboard issues
    else:
        event_details = awshealth.describe_event_details (
            eventArns=[strArn]
        )
        json_event_details = json.dumps(event_details, cls=DatetimeEncoder)
        parsed_event_details = json.loads (json_event_details)
        healthUpdates = (parsed_event_details['successfulSet'][0]['eventDescription']['latestDescription'])
        return healthUpdates

# send to chime function
def send_webhook(updatedOn, strStartTime, strEndTime, event, awsRegion, decodedWebHook, healthUpdates, affectedAccounts, affectedEntities):
    if len(affectedEntities) >= 1:
        affectedEntities = ", ".join(affectedEntities)
    if len(affectedAccounts) >= 1:
        affectedAccounts = ", ".join(affectedAccounts)
    else:
        affectedAccounts = "All accounts in region"
    message = str("/md" + "\n" + ":rotating_light:" + " **AWS Health Org View Alert** " + ":rotating_light:" + "\n"
      "---" + "\n"
      "**Account(s)**: " + affectedAccounts + "\n"
      "**Resource(s)**: " + affectedEntities + "\n"
      "**Service**: " + event['service'] + "\n"
      "**Region**: " + event['region'] + "\n" 
      "**Start Time (UTC)**: " + strStartTime + "\n"
      "**End Time (UTC)**: " + strEndTime + "\n"
      "**Posted Time (UTC)**: " + updatedOn + "\n"
      "**Status**: " + event['statusCode'] + "\n"
      "**Updates:**" + "\n" + healthUpdates
      )

    json.dumps(message)
    chime_message = {'Content': message}
    req = Request(decodedWebHook, data=json.dumps(chime_message).encode("utf-8"), headers={"content-type": "application/json"})
    try:
      response = urlopen(req)
      response.read()
      print("Message sent to chime: ", json.dumps(chime_message))
    except HTTPError as e:
       print("Request failed : ", e.code, e.reason)
    except URLError as e:
       print("Server connection failed: ", e.reason)

# time encoder class
class DatetimeEncoder(json.JSONEncoder):
    def default(self, obj):
        try:
            return super(DatetimeEncoder, obj).default(obj)
        except TypeError:
            return str(obj)

# main function
def lambda_handler(event, context):
    intHours = os.environ['searchback']
    intHours = int(intHours)*3600
    dictRegions = os.environ['regions']
    encryptedWebHook = os.environ['encryptedWebHook']
    ddbTable = os.environ['ddbTable']
    awsRegion = os.environ['AWS_DEFAULT_REGION']
    
    if dictRegions != "":
        dictRegions = dictRegions.replace("'","")
        dictRegions = list(dictRegions.split(",")) 
    
    # set standard date time format used throughout
    strDTMFormat2 = "%Y-%m-%d %H:%M:%S"
    strDTMFormat = '%s'

    config = Config(
        retries = dict(
            max_attempts = 10 # org view apis have a lower tps than the single
                              # account apis so we need to use larger
                              # backoff/retry values than than the boto defaults
        )
    )
    # creates health object as client.  AWS health only has a us-east-1 endpoint currently    
    awshealth = boto3.client('health', region_name='us-east-1', config=config)
    dynamodb = boto3.resource("dynamodb")
    kms = boto3.client('kms')
    print("boto3 version:"+boto3.__version__)
    
    response = kms.decrypt(CiphertextBlob=b64decode(encryptedWebHook))['Plaintext']
    string_response = response.decode('ascii')
    decodedWebHook = "https://" + string_response

    HealthIssuesTable = dynamodb.Table(ddbTable)

    if dictRegions != "":
        strFilter = {
                'regions':
                    dictRegions
    	}
    else:
        strFilter = {}

    response = awshealth.describe_events_for_organization (
        filter=
            strFilter
        ,
    )
    event_paginator = awshealth.get_paginator('describe_events_for_organization')
    event_page_iterator = event_paginator.paginate(filter=strFilter)
    for response in event_page_iterator:
        json_pre = json.dumps(response, cls=DatetimeEncoder)
        json_events = json.loads (json_pre)

        events = json_events.get('events')
        print("Event received: ", json.dumps(events))
        for event in events :
            strEventTypeCode = event['eventTypeCode']
            strArn = (event['arn'])
            # configure times
            strUpdate = parser.parse((event['lastUpdatedTime']))
            strUpdate = strUpdate.strftime(strDTMFormat)
            now = datetime.strftime(datetime.now(),strDTMFormat)
            strStartTime = parser.parse((event['startTime']))
            strStartTime = strStartTime.strftime(strDTMFormat2)
            if 'endTime' in event:
                strEndTime = parser.parse((event['endTime']))
                strEndTime = strEndTime.strftime(strDTMFormat2)
            else:
                strEndTime = "None given"
            
            if diff_dates(strUpdate, now) < int(intHours):
                try:
                    response = HealthIssuesTable.get_item(
                        Key = {
                            'arn' : strArn
                        }
                )
                except ClientError as e:
                    print(e.response['Error']['Message'])
                else:
                    isItemResponse = response.get('Item')
                    if isItemResponse == None:
                        print (datetime.now().strftime(strDTMFormat2)+": record not found")
                        update_ddb(HealthIssuesTable, strArn, strUpdate, now, intHours)
                        affectedAccounts = get_healthAccounts (awshealth, event, strArn, awsRegion)
                        healthUpdates = get_healthUpdates(awshealth, event, strArn, awsRegion, affectedAccounts)
                        affectedEntities = get_healthEntities(awshealth, event, strArn, awsRegion, affectedAccounts)
                        send_webhook(datetime.now().strftime(strDTMFormat2), strStartTime, strEndTime, event, awsRegion, decodedWebHook, healthUpdates, affectedAccounts, affectedEntities)
                    else:
                        item = response['Item']
                        if item['lastUpdatedTime'] != strUpdate:
                          print (datetime.now().strftime(strDTMFormat2)+": last Update is different")
                          update_ddb(HealthIssuesTable, strArn, strUpdate, now, intHours)
                          affectedAccounts = get_healthAccounts (awshealth, event, strArn, awsRegion)                      
                          healthUpdates = get_healthUpdates(awshealth, event, strArn, awsRegion, affectedAccounts)
                          affectedEntities = get_healthEntities(awshealth, event, strArn, awsRegion, affectedAccounts)
                          send_webhook(datetime.now().strftime(strDTMFormat2), strStartTime, strEndTime, event, awsRegion, decodedWebHook, healthUpdates, affectedAccounts, affectedEntities)