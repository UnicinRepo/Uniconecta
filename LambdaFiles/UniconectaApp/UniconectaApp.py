import json

def lambda_handler(event, context): #45666
    return {
        'statusCode': 200,
        'body': json.dumps('Hello from CloudMan (Python)!')
    }