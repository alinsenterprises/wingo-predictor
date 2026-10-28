#!/bin/sh
# create_lightsail_instance.sh
#
# Uses the AWS creds available in this shell (AWS_ACCESS_KEY_ID /
# AWS_SECRET_ACCESS_KEY) to create one Mumbai Lightsail instance and
# prepares it for the install-lightsail.sh handoff.
#
# What it does:
#   1. pick a Mumbai-available Ubuntu blueprint
#   2. pick a Mumbai-available bundle (Nano / Micro / Small)
#   3. create the Lightsail key pair for SSH
#   4. create the instance in ap-south-1
#   5. wait until the instance is running
#  6. print the public IP and the SSH command
#
# What it does NOT do (must be done from your machine or a later step):
#   - upload the project tarball
#   - write .env on the instance
#   - run install-lightsail.sh
#
# If you want a different bundle, edit BUNDLE below.

set -eu

INSTANCE_NAME="ALins_Agent"
REGION="ap-south-1"
AVAILABILITY="ap-south-1a"

BLUEPRINT="ubuntu_24_04"
BUNDLE="nano_3_0"
KEY_NAME="ALins_Agent_key"

AWS="aws --region $REGION"

echo "==> instance: $INSTANCE_NAME"
echo "==> region:   $REGION"
echo "==> blueprint: $BLUEPRINT"
echo "==> bundle:   $BUNDLE"
echo "==> key:      $KEY_NAME"
echo ""

# 1. confirm the blueprint exists and is available in this region
echo "==> validating blueprint + bundle"
$AWS lightsail get-blueprints --query "blueprints[?blueprintId=='$BLUEPRINT'].[blueprintId,name,version]" --output table 2>&1 | sed -n '1,20p'

$AWS lightsail get-bundles --query "bundles[?bundleId=='$BUNDLE'].[bundleId,name,computeTypeName,cpuCount,memorySizeInGb]" --output table 2>&1 | sed -n '1,20p'

# 2. create the SSH key pair
echo ""
echo "==> creating Lightsail key pair: $KEY_NAME"
$AWS lightsail create-key-pair --key-pair-name "$KEY_NAME" --output json 2>&1 \
  | python3 -c "import json,sys; d=json.load(sys.stdin); k=d.get('keyPair',{}); print('keyPairName=', k.get('name')); print('arn=', k.get('arn'))" 2>&1 | sed -n '1,20p'

# 3. create the instance
echo ""
echo "==> creating instance in $REGION"
$AWS lightsail create-instances \
  --instance-names "$INSTANCE_NAME" \
  --blueprint-id "$BLUEPRINT" \
  --bundle-id "$BUNDLE" \
  --key-pair-name "$KEY_NAME" \
  --region-name "$REGION" \
  --availability-zone "$AVAILABILITY" \
  --output json 2>&1 \
  | python3 -c "import json,sys; d=json.load(sys.stdin); ops=d.get('operations',[]); [print(o.get('operationType'), o.get('resourceName'), o.get('status')) for o in ops]" 2>&1 | sed -n '1,20p'

# 4. wait until it is running
echo ""
echo "==> waiting for instance to be running"
$AWS lightsail wait instance-running --instance-names "$INSTANCE_NAME" 2>&1 | sed -n '1,20p' || {
  echo "!!  instance did not reach running state" >&2
  echo "    describe it with:" >&2
  echo "      aws lightsail get-instance --instance-name $INSTANCE_NAME --region $REGION --output json" >&2
  exit 1
}

# 5. print result
echo ""
echo "==> instance state"
$AWS lightsail get-instance --instance-name "$INSTANCE_NAME" --query "instance.[name,state,publicIpAddress,privateIpAddress,blueprintId,bundleId,karn]" --output table 2>&1 | sed -n '1,30p'

echo ""
echo "==> public IP and SSH command"
$AWS lightsail get-instance --instance-name "$INSTANCE_NAME" --query "instance.publicIpAddress" --output text 2>&1 | sed -n '1,20p' | {
  read -r PUBLIC_IP
  if [ -n "$PUBLIC_IP" ] && [ "$PUBLIC_IP" != "None" ]; then
    echo "PUBLIC_IP=$PUBLIC_IP"
    echo ""
    echo "ssh command from your machine:"
    echo "  ssh -i ~/.ssh/ALins_Agent_key.pem ubuntu@$PUBLIC_IP"
    echo ""
    echo "Next step on the instance:"
    echo "  ssh -i ~/.ssh/ALins_Agent_key.pem ubuntu@$PUBLIC_IP"
    echo "    'sudo mkdir -p /opt/wingo-analyzer && sudo chown ubuntu:ubuntu /opt/wingo-analyzer'"
    echo ""
    echo "Then from your project root:"
    echo "  scp -i ~/.ssh/ALins_Agent_key.pem payload.tgz ubuntu@$PUBLIC_IP:/home/ubuntu/"
    echo "  ssh -i ~/.ssh/ALins_Agent_key.pem ubuntu@$PUBLIC_IP"
    echo "    'cd /opt/wingo-analyzer && tar xzf /home/ubuntu/payload.tgz'"
    echo "    'bash ./deploy/install-lightsail.sh'"
  else
    echo "!!  no public IP yet" >&2
  fi
}
