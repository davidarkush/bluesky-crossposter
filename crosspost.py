from atproto import Client
import tweepy
from mastodon import Mastodon
from datetime import datetime, timedelta
from auth import *
from paths import *
import toggle
import json, os, urllib.request, random, string, shutil

date_in_format = '%Y-%m-%dT%H:%M:%S'

# Setting up connections to bluesky, twitter and mastodon

bsky = Client()
bsky.login(bsky_handle, bsky_password)

# After changes in twitters API we need to use tweepy.Client to make posts as it uses version 2.0 of the API.
# However, uploading images is still not included in 2.0, so for that we need to use tweepy.API, which uses
# the previous version.
if toggle.Twitter:
    twitter = tweepy.Client(consumer_key=TWITTER_APP_KEY,
                        consumer_secret=TWITTER_APP_SECRET,
                        access_token=TWITTER_ACCESS_TOKEN,
                        access_token_secret=TWITTER_ACCESS_TOKEN_SECRET)

    tweepy_auth = tweepy.OAuth1UserHandler(TWITTER_APP_KEY, TWITTER_APP_SECRET, TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_TOKEN_SECRET)
    twitter_images = tweepy.API(tweepy_auth)

if toggle.Mastodon:
    mastodon = Mastodon(
        access_token = MASTODON_TOKEN,
        api_base_url = MASTODON_INSTANCE
    )

# Getting posts from bluesky

def getPosts():
    posts = {}
    # Getting feed of user
    profile_feed = bsky.bsky.feed.get_author_feed({'actor': bsky_handle})
    for feed_view in profile_feed.feed:
        # Currently it's not possible to get images included in quote posts due to a limitation
        # in the atproto python library. While awaiting a fix for that, this function
        # checks for posts that should have images but which it can't access and if that
        # is the case we skip the post.
        if imageFail(feed_view.post):
            continue;
        # Post type "post" means it is not a quote post.
        postType = "post"
        # If post has an embed of type record it is a quote post, and should not be crossposted
        cid = feed_view.post.cid
        text = feed_view.post.record.text
        timestamp = datetime.strptime(feed_view.post.indexedAt.split(".")[0], date_in_format) + timedelta(hours = 2)
        # Setting replyToUser to the same as user handle and only changing it if the tweet is an actual reply.
        # This way we can just check if the variable is the same as the user handle later and send through
        # both tweets that are not replies, and posts that are part of a thread.
        replyToUser = bsky_handle
        replyTo = ""
        # Checking if post is a quote tweet
        if feed_view.post.embed and hasattr(feed_view.post.embed, "record"):
            replyToUser, replyTo = getQuotePost(feed_view.post.embed.record)
            postType = "quote"
        # Checking if post is regular reply
        elif feed_view.post.record.reply:
            replyToUser = feed_view.reply.parent.author.handle
            replyTo = feed_view.post.record.reply.parent.cid
        # Checking if post is by user (i.e. not a repost), withing the last 12 hours and either not a reply or a reply in a thread.
        if feed_view.post.author.handle == bsky_handle and timestamp > datetime.now() - timedelta(hours = 12) and replyToUser == bsky_handle:
            # Fetching images if there are any in the post
            imageData = ""
            images = []
            if feed_view.post.embed and hasattr(feed_view.post.embed, "images"):
                imageData = feed_view.post.embed.images
            elif feed_view.post.embed and hasattr(feed_view.post.embed, "media") and postType == "quote":
                imageData = feed_view.post.embed.media.images
            if imageData:
                for image in imageData:
                    images.append({"url": image.fullsize, "alt": image.alt})
            postInfo = {
                "text": text,
                "replyTo": replyTo,
                "images": images,
                "type": postType
            }
            # Saving post to posts dictionary
            posts[cid] = postInfo;
    return posts

# Function for getting included images. If no images are included, an empty list will be returned, 
# and the posting functions will know not to include any images.
def getImages(images):
    localImages = []
    for image in images:
        # Getting alt text for image. If there is none this will be an empty string.
        alt = image["alt"]
        # Giving the image just a random filename
        filename = ''.join(random.choice(string.ascii_lowercase) for i in range(10)) + ".jpg"
        filename = imagePath + filename
        # Downloading fullsize version of image
        urllib.request.urlretrieve(image["url"], filename)
        # Saving image info in a dictionary and adding it to the list.
        imageInfo = {
            "filename": filename,
            "alt": alt
        }
        localImages.append(imageInfo)
    return localImages

def getQuotePost(post):
    if isinstance(post, dict):
        user = post["record"]["author"]["handle"]
        cid = post["record"]["cid"]
    elif hasattr(post, "author"):
        user = post.author.handle
        cid = post.cid
    else:
        user = post.record.author.handle
        cid = post.record.cid
    return user, cid

def imageFail(post):
    if (post.embed and (hasattr(post.record.embed, "image") or hasattr(post.record.embed, "media"))
        and not hasattr(post.embed, "images")):
        return True
    else:
        return False

def post(posts):
    # Running through the posts dictionary reversed, to get oldest posts first.
    for cid in reversed(list(posts.keys())):
        # Checking if the post is already in the database, and in that case getting the IDs for the post
        # on twitter and mastodon. If one or both of these IDs are empty, post will be sent.
        tweetId = ""
        tootId = ""
        if cid in database:
            tweetId = database[cid]["twitterId"]
            tootId = database[cid]["mastodonId"]
        text = posts[cid]["text"]
        replyTo = posts[cid]["replyTo"]
        images = posts[cid]["images"]
        postType = posts[cid]["type"]
        tweetReply = ""
        tootReply = ""
        # If it is a reply, we get the IDs of the posts we want to reply to from the database.
        # If post is not found in database, we can't continue the thread on mastodon and twitter,
        # and so we skip it.
        if replyTo in database:
            tweetReply = database[replyTo]["twitterId"]
            tootReply = database[replyTo]["mastodonId"]
        elif replyTo and replyTo not in database:
            continue
        # If either tweet or toot has not previously been posted, we download images (given the post includes images).
        if not tweetId or not tootId:
            images = getImages(images)
        # Trying to post to twitter and mastodon. If posting fails the post ID for each service is set to an
        # empty string, letting the code know it should try again next time the code is run.
        if not tweetId:
            try:
                tweetId = tweet(text, tweetReply, images, postType)
            except Exception as error:
                print(error)
                tweetId = ""
        # Mastodon does not have a quote retweet function, so those will just be sent as replies.
        if not tootId:
            try:
                tootId = toot(text, tootReply, images)
            except Exception as error:
                print(error)
                tootId = ""
        # Saving post to database
        jsonWrite(cid, tweetId, tootId)

# Function for posting tweets
def tweet(post, replyTo, images, postType):
    if not toggle.Twitter:
        return;
    mediaIds = []
    # If post includes images, images are uploaded so that they can be included in the tweet
    if images:
        mediaIds = []
        for image in images:
            filename = image["filename"]
            alt = image["alt"]
            res = twitter_images.media_upload(filename)
            id = res.media_id
            # If alt text was added to the image on bluesky, it's also added to the image on twitter.
            if alt:
                writeLog("Uploading image " + filename + " with alt: " + alt + " to twitter")
                twitter_images.create_media_metadata(id, alt)
            mediaIds.append(id)
    # I wanted to make this part a little neater, but didn't get it to work and gave up. So here we are.
    # If post is both reply and has images it is posted as both a reply and with images (duh), if it's
    # a quote with images it's posted as that. If just either of the three it is posted as just that, 
    # and if neither it is just posted as a text post.
    if replyTo and mediaIds and postType == "quote":
        a = twitter.create_tweet(text=post, quote_tweet_id=replyTo, media_ids=mediaIds)
    elif replyTo and mediaIds:
        a = twitter.create_tweet(text=post, in_reply_to_tweet_id=replyTo, media_ids=mediaIds)
    elif postType == "quote":
        a = twitter.create_tweet(text=post, quote_tweet_id=replyTo)
    elif replyTo:
        a = twitter.create_tweet(text=post, in_reply_to_tweet_id=replyTo)
    elif mediaIds:
        a = twitter.create_tweet(text=post, media_ids=mediaIds)
    else:
        a = twitter.create_tweet(text=post)
    writeLog("Posted to twitter")
    id = a[0]["id"]
    return id

# More or less the exact same function as for tweeting, but for tooting.
def toot(post, replyTo, images):
    if not toggle.Mastodon:
        return;
    mediaIds = []
    # If post includes images, images are uploaded so that they can be included in the toot
    if images:
        for image in images:
            filename = image["filename"]
            alt = image["alt"]
            # If alt text was added to the image on bluesky, it's also added to the image on mastodon,
            # otherwise it will be uploaded without alt text.
            if alt:
                writeLog("Uploading image " + filename + " with alt: " + alt + " to mastodon")
                res = mastodon.media_post(filename, description=alt)
            else:
                writeLog("Uploading image " + filename)
                res = mastodon.media_post(filename)
            mediaIds.append(res.id)
    # I wanted to make this part a little neater, but didn't get it to work and gave up. So here we are.
    # If post is both reply and has images it is posted as both a reply and with images (duh). 
    # If just either of the two it is posted with just that, and if neither it is just posted as a text post.
    if replyTo and mediaIds:
        a = mastodon.status_post(post, in_reply_to_id=replyTo, media_ids=mediaIds)
    elif replyTo:
        a = mastodon.status_post(post, in_reply_to_id=replyTo, visibility="unlisted")
    elif mediaIds:
        a = mastodon.status_post(post, media_ids=mediaIds, visibility="unlisted")
    else:
        a = mastodon.status_post(post, visibility="unlisted")
    writeLog("Posted to mastodon")
    id = a["id"]
    return id

# Function for writing new lines to the database
def jsonWrite(skeet, tweet, toot):
    ids = { 
        "twitterId": tweet,
        "mastodonId": toot
    }
    # When running, the code saves the database to memory, so instead of just saving the post to the database file,
    # we also save it to the open database. This also overwrites the version of the post in memory in case
    # an ID that was missing because of a previous failure. 
    database[skeet] = ids
    row = {
        "skeet": skeet,
        "ids": ids
        }
    jsonString = json.dumps(row)
    # If the database file exists we want to append to it, otherwise we create it anew.
    if os.path.exists(databasePath):
        append_write = 'a'
    else:
        append_write = 'w'
    # Skipping adding posts to db file if they are already in it.
    if not isInDB(jsonString):
        writeLog("Adding to database: " + jsonString)
        file = open(databasePath, append_write)
        file.write(jsonString + "\n")
        file.close()

# Function for reading database file and saving values in a dictionary
def jsonRead():
    database = {}
    if os.path.exists(databasePath):
        with open(databasePath, 'r') as file:
            for line in file:
                jsonLine = json.loads(line)
                database[jsonLine["skeet"]] = jsonLine["ids"]
    return database;

# Function for checking if a line is already in the database-file
def isInDB(line):
     with open(databasePath, 'r') as file:
        content = file.read()
        if line in content:
            return True
        else:
            return False

# Function for writing to the log file
def writeLog(message):
    now = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    date = datetime.now().strftime("%y%m%d")
    message = str(now) + ": " + message + "\n"
    print(message)
    if not toggle.Logging:
        return;
    log = logPath + date + ".log"
    if os.path.exists(log):
        append_write = 'a'
    else:
        append_write = 'w'
    dst = open(log, append_write)
    dst.write(message)
    dst.close()

# Cleaning up downloaded images
def cleanup():
    for filename in os.listdir(imagePath):
        file_path = os.path.join(imagePath, filename)
        try:
            if os.path.isfile(file_path) or os.path.islink(file_path):
                os.unlink(file_path)
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)
        except Exception as e:
            writeLog('Failed to delete %s. Reason: %s' % (file_path, e))

# Since we are working with a version of the database in memory, at the end of the run
# we completely overwrite the database on file with the one in memory.
# This does kind of make it uneccessary to write each new post to the file while running,
# but in case the program fails halfway through it gives us kind of a backup.
def saveDB():
    writeLog("Saving new database")
    append_write = "w"
    for skeet in database:
        row = {
            "skeet": skeet,
            "ids": database[skeet]
        }
        jsonString = json.dumps(row)
        file = open(databasePath, append_write)
        file.write(jsonString + "\n")
        file.close()
        append_write = "a"

# Function for counting lines in a file
def countLines(file):
    with open(file, 'r') as file:
        for count, line in enumerate(file):
            pass
    return count

# Every twelve hours a backup of the database is saved, in case something happens to the live database.
# If the live database contains fewer lines than the backup it means something has probably gone wrong,
# and before the live database is saved as a backup, the current backup is saved as a new file, so that
# it can be recovered later.
def dbBackup():
    if os.path.isfile(backupPath) and datetime.fromtimestamp(os.stat(backupPath).st_mtime) > datetime.now() - timedelta(hours = 24):
        return
    if os.path.isfile(backupPath):
        if countLines(backupPath) < countLines(databasePath):
            os.remove(backupPath)
        else:
            date = datetime.now().strftime("%y%m%d")
            os.rename(backupPath, backupPath + "_" + date)
            writeLog("Current backup file contains more entries than current live database, backup saved")
    shutil.copyfile(databasePath, backupPath)
    writeLog("Backup of database taken")
            
# Here the whole thing is run
database = jsonRead()
posts = getPosts()
post(posts)
saveDB()
cleanup()
dbBackup()
if not posts:
	writeLog("No new posts found.")
