#!/bin/bash
echo "Creating docker volumes for SEEK"
docker volume create --name=seek-filestore
docker volume create --name=seek-mysql-db
docker volume create --name=seek-solr-data
docker volume create --name=seek-cache

echo "Downloading docker-compose.yml and db.env"
#wget https://raw.githubusercontent.com/seek4science/seek/seek-1.18/docker-compose.yml
wget https://raw.githubusercontent.com/abecam/prepare-seek4science/main/TestSeekDockerCompose/docker-compose.yml
mkdir docker
cd docker
wget https://raw.githubusercontent.com/seek4science/seek/seek-1.18/docker/db.env

#mkdir solr
#cd solr
#mkdir seek
#cd seek
#mkdir conf
#cd conf
#wget https://raw.githubusercontent.com/seek4science/seek/refs/heads/seek-1.18/solr/seek/conf/solrconfig.xml

cd ..
echo "Starting SEEK containers"
docker compose up -d

echo "Waiting for SEEK to be ready"
until docker logs seek | grep -q "Listening on"; do
  sleep 5
done
echo "SEEK is ready"

echo "Creating initial admin user"
wget https://raw.githubusercontent.com/abecam/prepare-seek4science/main/CreateInitialuser.yml
ansible-playbook CreateInitialuser.yml

echo "Fetching and pushing data from investigation"
wget https://raw.githubusercontent.com/abecam/prepare-seek4science/main/FetchPushFromInvestigation.py
python3 FetchPushFromInvestigation.py