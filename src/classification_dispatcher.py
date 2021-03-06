import csv
import os
import os.path
import json
import re
from elasticsearch import Elasticsearch
from muppet import DurableChannel, RemoteChannel

__name__ = "classification_dispatcher"

class ClassificationDispatcher:
  
  def __init__(self, config, processingStartIndex, processingEndIndex):
    self.config = config
    self.logger = config["logger"]
    self.esClient = Elasticsearch(config["elasticsearch"]["host"] + ":" + str(config["elasticsearch"]["port"]))
    self.config["processingStartIndex"] = processingStartIndex
    self.config["processingEndIndex"] = processingEndIndex
    self.bagOfPhrases = {}
    
    self.corpusSize = 0
    self.processorIndex = config["processor"]["index"]
    self.processorType = config["processor"]["type"]
    self.processorPhraseType = config["processor"]["type"]+"__phrase"
    self.processingPageSize = config["processingPageSize"]
    config["processor_phrase_type"] = self.processorPhraseType
    
    self.featureNames = map(lambda x: x["name"], config["generator"]["features"])
    for module in config["processor"]["modules"]:
      self.featureNames = self.featureNames + map(lambda x: x["name"], module["features"])

    self.totalPhrasesDispatched = 0
    self.phrasesClassified = 0
    self.phrasesNotClassified = 0
    self.timeout = 86400000
    self.dispatcherName = "bayzee.classification.dispatcher"
    if processingEndIndex != None:
      self.dispatcherName += "." + str(processingStartIndex) + "." + str(processingEndIndex)
    self.workerName = "bayzee.classification.worker"
    
    # creating generation dispatcher
    self.classificationDispatcher = DurableChannel(self.dispatcherName, config, self.timeoutCallback)

    #remote channel intialisation
    self.controlChannel = RemoteChannel(self.dispatcherName, config)

  def dispatchToClassify(self):
    processorIndex = self.config["processor"]["index"]
    phraseProcessorType = self.config["processor"]["type"] + "__phrase"
    nextPhraseIndex = 0
    if self.config["processingStartIndex"] != None: nextPhraseIndex = self.config["processingStartIndex"]
    endPhraseIndex = -1
    if self.config["processingEndIndex"] != None: endPhraseIndex = self.config["processingEndIndex"]
    
    if endPhraseIndex != -1 and self.processingPageSize > (endPhraseIndex - nextPhraseIndex):
      self.processingPageSize = endPhraseIndex - nextPhraseIndex + 1
    
    while True:
      phrases = self.esClient.search(index=processorIndex, doc_type=phraseProcessorType, body={"from": nextPhraseIndex,"size": self.processingPageSize, "query":{"match_all":{}},"sort":[{"phrase__not_analyzed":{"order":"asc"}}]}, fields=["_id"])
      if len(phrases["hits"]["hits"]) == 0: break
      self.totalPhrasesDispatched += len(phrases["hits"]["hits"])
      floatPrecision = "{0:." + str(self.config["generator"]["floatPrecision"]) + "f}"
      self.logger.info("Classifying phrases from " + str(nextPhraseIndex) + " to " + str(nextPhraseIndex+len(phrases["hits"]["hits"])) + " phrases...")
      for phraseData in phrases["hits"]["hits"]:
        self.logger.info("Dispatched phrase " + phraseData["_id"])
        content = {"phraseId": phraseData["_id"], "type": "classify", "count": 1, "from": self.dispatcherName}
        self.classificationDispatcher.send(content, self.workerName, self.timeout)
  
      nextPhraseIndex += len(phrases["hits"]["hits"])
      if endPhraseIndex != -1 and nextPhraseIndex >= endPhraseIndex: break
    
    self.logger.info("Dispatched " + str(self.totalPhrasesDispatched) + " phrases")
    
    while True:
      message = self.classificationDispatcher.receive()
      if "phraseId" in message["content"] and message["content"]["phraseId"] > 0:
        self.phrasesClassified += 1
        self.classificationDispatcher.close(message)
        self.logger.info("Classified phrase " + message["content"]["phraseId"] + " " + str(self.phrasesClassified) + "/" + str(self.totalPhrasesDispatched))
      
      if (self.phrasesClassified + self.phrasesNotClassified) >= self.totalPhrasesDispatched:
        self.controlChannel.send("dying")
        self.classificationDispatcher.end()
        break
    
    self.__terminate()


  def timeoutCallback(self, message):
    self.logger.info("Message timed out: " + str(message))
    if message["content"]["count"] < 5:
      message["content"]["count"] += 1
      self.classificationDispatcher.send(message["content"], self.workerName, self.timeout)
    else:
      #log implementation yet to be done for expired phrases
      self.phrasesNotClassified += 1
      if self.phrasesNotClassified == self.totalPhrasesDispatched or (self.phrasesClassified + self.phrasesNotClassified) == self.totalPhrasesDispatched:
        self.__terminate()

  def __terminate(self):
    self.logger.info(str(self.totalPhrasesDispatched) + " total dispatched")
    self.logger.info(str(self.phrasesClassified) + " classified")
    self.logger.info(str(self.phrasesNotClassified) + " failed to classify")
    self.logger.info("Classification complete")
    self.logger.info("Terminating classification dispatcher")