import copy
import datetime
import hashlib
import os
import random
import re
import sys
import time
from multiprocessing import Pool

from jinja2 import Template
from pymongo import MongoClient

from .parser import rssParser, rssFullParser, objParser
from .sender import loadSender, validateSender, initSender
from .telegraph import generateTelegraph
from .utils import default_user_agent, execFunc, pickleSSL


class FR2T:
    def __init__(self, config, rss):
        self.rss = rss
        self.database_url = os.getenv("DATABASE") or config["database_url"]
        self.expire_time = os.getenv("EXPIRE_TIME") or config["expire_time"] or "30d"
        self.user_agent = (
                os.getenv("USER-AGENT") or config.get("user-agent") or default_user_agent
        )
        self.telegraph_access_token = (
                os.getenv("TELEGRAPH_ACCESS_TOKEN") or config["telegraph_access_token"]
        )

        self.valid_send, self.sender = validateSender(loadSender(config))

        if self.valid_send == "no_valid":
            sys.exit("No Valid Sender!")

    def run(self):
        db = MongoClient(self.database_url)["RSS"]

        all_sub = db.list_collection_names()

        tmp_rss1 = []
        tmp_rss2 = []
        # If url is a list, split rss into multiple lists
        for r in self.rss["rss"]:
            url = r.get("url")
            if isinstance(url, str):
                tmp_rss1.append(r)
            elif isinstance(url, list):
                for u in url:
                    r["url"] = u
                    tmp_rss1.append(r)

        # Valid sender in rss
        for r in tmp_rss1:
            r["new_sub"] = r["name"] not in all_sub
            if self.valid_send != "multiple_valid":
                r["sendto"] = [self.valid_send]
            else:
                if not r.get("sendto"):
                    r["sendto"] = [self.sender.keys()[0]]
                else:
                    if isinstance(r["sendto"], str):
                        r["sendto"] = [r["sendto"]]

            tmp_sender = copy.deepcopy(self.sender)

            if r.get("sender"):
                for sd in r.get("sender"):
                    if tmp_sender.get(sd):
                        tmp_sender[sd].update(r["sender"][sd])
                    else:
                        tmp_sender[sd] = r["sender"][sd]

            r["sender"] = tmp_sender

            r["database_url"] = r.get("database_url") or self.database_url
            r["user_agent"] = r.get("user_agent") or self.user_agent
            r["telegraph_access_token"] = (
                    r.get("telegraph_access_token") or self.telegraph_access_token
            )

            tmp_rss2.append(r)

        with Pool(8) as p:
            p.map(ProcessRSS, tmp_rss2)

        print("Finished!")

    def purge(self):
        now_time = datetime.datetime.now()
        days = hours = 0
        if self.expire_time.endswith("y"):
            days = int(self.expire_time.rstrip("y")) * 365

        if self.expire_time.endswith("m"):
            days = int(self.expire_time.rstrip("m")) * 30

        if self.expire_time.endswith("d"):
            days = int(self.expire_time.rstrip("d"))

        if self.expire_time.endswith("h"):
            hours = int(self.expire_time.rstrip("h"))

        expired_time = now_time - datetime.timedelta(days=days, hours=hours)
        expired_timestamp = datetime.datetime.timestamp(expired_time)

        deleted_num = 0
        db = MongoClient(self.database_url)["RSS"]
        col_list = db.list_collection_names()
        for col_name in col_list:
            print(f"开始清理: {col_name}")
            col = db[col_name]
            purge_rule = {"create_time": {"$lt": expired_timestamp}}

            deleted_result = col.delete_many(purge_rule)
            deleted_num += deleted_result.deleted_count

        print(f"已删除 {deleted_num} 个记录！")


class ProcessRSS:
    def __init__(self, rss):
        self.rss = rss

        pickleSSL()
        self.main()

    def main(self):
        db = MongoClient(self.rss["database_url"])["RSS"]

        if self.rss.get("fulltext"):
            rss_content = rssFullParser(self.rss["url"])
        else:
            rss_content = rssParser(self.rss["url"], self.rss["user_agent"])

        if not rss_content:
            self.handleExpire()
        else:
            db["Expire"].update_one(
                {"url": self.rss["url"]},
                {"$set": {"expired": 0}},
            )

            id_map = set()

            for content in rss_content:
                if self.handleFilter(content):
                    result = self.handleMatcher(content)

                    id = self.handleID(content)

                    if id not in id_map:
                        id_map.add(id)

                        posted = db[self.rss["name"]].find_one({"id": id})

                        telegraph_url, telegraph_content = self.handleTelegraph(
                            posted,
                            content,
                        )

                        if telegraph_url != False:
                            result["telegraph"] = telegraph_url

                            # To calculate text hash, ignore characters needing to be escaped
                            template = Template(self.rss["text"])

                            args = dict(
                                **result,
                                **content,
                                rss_name=self.rss["name"],
                                rss_url=self.rss["url"],
                                rss_content=telegraph_content,
                            )

                            text = template.render(args)
                            text_hash = hashlib.md5(text.encode()).hexdigest()

                            # Collection structure：
                            #   - _id: ObjectId
                            #   - create_time: Double
                            #   - edit_time: Double
                            #   - id: String
                            #   - text: String
                            #   - text_hash: String
                            #   - telegraph_url: String
                            #   - telegram_text_hash: String
                            #   - telegram_message_id: Int32
                            #   - telegram_exist: Int32 (1 or 0)
                            #   - telegram_send_success: Int32 (1 or 0)
                            #   ...

                            set_data = (
                                {"telegraph_url": telegraph_url}
                                if not posted
                                   or not posted.get("telegraph_url")
                                   and telegraph_url
                                else {}
                            )

                            if posted:
                                if posted["text_hash"] != text_hash:
                                    set_data["text"] = text
                                    set_data["text_hash"] = text_hash

                                for st in self.rss["sendto"]:
                                    sen = initSender(st, self.rss["sender"][st])
                                    msg = sen.render(self.rss["text"], args)

                                    if not posted[st + "_send_success"]:
                                        result_id = sen.send(msg)
                                        if result_id:
                                            set_data[st + "_text_hash"] = text_hash
                                            set_data[st + "_message_id"] = result_id
                                            set_data[st + "_exist"] = 1
                                            set_data[st + "_send_success"] = 1

                                            print(
                                                "{} sent 1 message: TEXT {} in {}.".format(
                                                    st.capitalize(),
                                                    text_hash,
                                                    self.rss["name"],
                                                )
                                            )

                                    elif (
                                            posted[st + "_exist"]
                                            and posted[st + "_text_hash"] != text_hash
                                    ):
                                        edit_result = sen.edit(
                                            posted[st + "_message_id"], msg
                                        )
                                        if edit_result == 2:
                                            set_data[st + "_text_hash"] = text_hash
                                            print(
                                                "{} edited 1 message: TEXT {} in {}.".format(
                                                    st.capitalize(),
                                                    text_hash,
                                                    self.rss["name"],
                                                )
                                            )

                                        elif edit_result == 1:
                                            set_data[st + "_exist"] = 0
                                            print(
                                                "{} edited 1 message: TEXT {} in {} (doesn't exist).".format(
                                                    st.capitalize(),
                                                    text_hash,
                                                    self.rss["name"],
                                                )
                                            )

                                if set_data:
                                    set_data.update({"edit_time": time.time()})
                                    db[self.rss["name"]].update_one(
                                        {"_id": posted["_id"]},
                                        {"$set": set_data},
                                    )

                            else:
                                set_data.update(
                                    {
                                        "id": id,
                                        "text": text,
                                        "text_hash": text_hash,
                                        "create_time": time.time(),
                                        "edit_time": time.time(),
                                    }
                                )

                                if self.rss["new_sub"]:
                                    for st in self.rss["sendto"]:
                                        set_data[st + "_text_hash"] = text_hash
                                        set_data[st + "_message_id"] = -1
                                        set_data[st + "_exist"] = 0
                                        set_data[st + "_send_success"] = 1

                                        print(
                                            "{} sent 1 message: TEXT {} in {} (initial).".format(
                                                st.capitalize(),
                                                text_hash,
                                                self.rss["name"],
                                            )
                                        )
                                else:
                                    for st in self.rss["sendto"]:
                                        sen = initSender(st, self.rss["sender"][st])
                                        msg = sen.render(self.rss["text"], args)
                                        result_id = sen.send(msg)

                                        set_data[st + "_text_hash"] = text_hash
                                        set_data[st + "_message_id"] = result_id or -1
                                        set_data[st + "_exist"] = int(bool(result_id))
                                        set_data[st + "_send_success"] = int(
                                            bool(result_id)
                                        )

                                        print(
                                            "{} sent 1 message: TEXT {} in {}.".format(
                                                st.capitalize(),
                                                text_hash,
                                                self.rss["name"],
                                            )
                                        )

                                db[self.rss["name"]].insert_one(set_data)

    def handleExpire(self):
        url = self.rss["url"]
        db = MongoClient(self.rss["database_url"])["RSS"]

        expired_url = db["Expire"].find_one({"url": url})
        if expired_url:
            if expired_url["expired"] > 100:
                print(f"订阅 {url} 已失效")
            else:
                db["Expire"].update_one(
                    {"_id": expired_url["_id"]},
                    {"$set": {"expired": expired_url["expired"] + 1}},
                )
        else:
            db["Expire"].insert_one({"url": url, "expired": 1})

    def handleFilter(self, content):
        if self.rss.get("filters"):
            for filter in self.rss["filters"]:
                obj = objParser(content, filter["obj"])
                if filter.get("type") == "in":
                    if not re.search(filter["matcher"], obj):
                        return False
                else:
                    if re.search(filter["matcher"], obj):
                        return False

        return True

    def handleMatcher(self, content):
        result = {}

        if self.rss.get("rules"):
            for rule in self.rss["rules"]:
                obj = objParser(content, rule["obj"])

                if not rule.get("type") or rule["type"] == "regex":
                    matcher = re.compile(rule["matcher"])
                    matched = matcher.search(obj)

                    # If only one match is made, the match is returned,
                    # otherwise the matching string and all matches are returned
                    if len(matched.groups()) == 1:
                        matched = matched.groups()[0]
                    else:
                        tmp_matched = list(matched.groups())
                        tmp_matched.insert(0, matched.group())
                        matched = tmp_matched

                    result[rule["dest"]] = matched

                elif rule["type"] == "func":
                    result[rule["dest"]] = execFunc(obj, rule["matcher"])

        return result

    def handleID(self, content):
        id1_hash = hashlib.md5(self.rss["url"].encode()).hexdigest()

        id2 = content.get("id") or content.get("guid") or content.get("link")
        id2_hash = hashlib.md5(id2.encode()).hexdigest()

        return id1_hash + id2_hash

    def handleTelegraph(self, posted, content):
        def __generateTelegraph(
                title, telegraph_author, content, telegraph_access_token
        ):
            time.sleep(random.randint(1, 5))

            telegraph_url = generateTelegraph(
                telegraph_access_token,
                title,
                telegraph_author,
                content,
            )

            return telegraph_url

        telegraph_url = telegraph_content = ""

        if self.rss.get("telegraph") and self.rss.get("telegraph_access_token"):
            if self.rss.get("content"):
                obj = objParser(content, self.rss["content"]["obj"])
                telegraph_content = execFunc(obj, self.rss["content"]["matcher"])
            else:
                telegraph_content = (
                    content["content"][0]["value"]
                    if content.get("content")
                    else content["summary"]
                )

            telegraph_author = content.get("author") or "Anonymous"

            telegraph_author_title = content["title"]

            if not self.rss.get("new_sub"):
                post_flag = False
                if posted:
                    if not posted.get("telegraph_url"):
                        post_flag = True
                    else:
                        telegraph_url = posted["telegraph_url"]
                else:
                    post_flag = True

                if post_flag:
                    telegraph_url = __generateTelegraph(
                        telegraph_author_title,
                        telegraph_author,
                        telegraph_content,
                        self.rss["telegraph_access_token"],
                    )
            else:
                telegraph_url = "https://example.com"

        return telegraph_url, telegraph_content
